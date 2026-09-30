-- =============================================================================
-- Apuesta Total (simulado) — esquema de dominio
--
-- Modelo: exchange de apuestas, simplificado pero coherente.
--
--   Evento      -> partido (LOCAL, VISITANTE, fecha)
--   Mercado     -> 1X2, Handicap, Total de goles, Corners... (pertenece a un evento)
--   Selección   -> resultado concreto dentro de un mercado (LOCAL / DRAW / VISITANTE)
--   Cuota       -> precio offering, versionado. Se versiona porque un consumidor
--                  que leyó la cuota a las 20:00:05 tiene que poder liquidar
--                  contra ESE precio, no contra el actual.
--   Apuesta      -> wager de un usuario contra un mercado
--   Posición     -> lado (BACK/LAY) + tamaño + precioAlQueEntro
--   Wallet      -> saldo del usuario
--   Transacción -> ledger. Append-only: el saldo es un caché de la suma.
--
-- DECISIÓN: el ledger es la fuente de verdad del saldo, no la columna de
-- balance. Si se corrompe la fila del wallet, se puede reconstruir sumando el
-- ledger. En un producto de apuestas esto no es opcional: es lo que permite
-- auditar cuánto debe un usuario y por qué.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Usuarios
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id           BIGSERIAL PRIMARY KEY,
    username     TEXT        NOT NULL UNIQUE,
    country      CHAR(2)     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- cuenta en la que seolvable el saldo
    currency     CHAR(3)     NOT NULL DEFAULT 'EUR'
);

-- -----------------------------------------------------------------------------
-- Eventos (partidos)
--
-- status control el ciclo de vida. Es lo que gobierna el autotuner de la
-- simulación: cuando un evento pasa a IN_PLAY, sus cuotas se mueven.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    competition  TEXT        NOT NULL,
    starts_at    TIMESTAMPTZ NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'SCHEDULED'
                             CHECK (status IN ('SCHEDULED','IN_PLAY','FINISHED','ABANDONED')),
    -- minutos jugados al momento de la apuesta. Es lo que hace que dos apuestas
    -- sobre el mismo mercado puedan tener precios distintos y coherentes.
    clock_minute SMALLINT    NOT NULL DEFAULT 0,
    score_home   SMALLINT    NOT NULL DEFAULT 0,
    score_away   SMALLINT    NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_starts_at_idx  ON events (starts_at);
CREATE INDEX IF NOT EXISTS events_status_idx     ON events (status);

-- -----------------------------------------------------------------------------
-- Mercados
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
    id           BIGSERIAL PRIMARY KEY,
    event_id     BIGINT      NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,
    market_type  TEXT        NOT NULL
                             CHECK (market_type IN ('MATCH_1X2','HANDICAP','OVER_UNDER','BOTH_TO_SCORE')),
    -- línea del handicap / total. NULL para 1X2.
    line         NUMERIC(6,2),
    status       TEXT        NOT NULL DEFAULT 'OPEN'
                             CHECK (status IN ('OPEN','SUSPENDED','SETTLED','VOID')),
    settled_at   TIMESTAMPTZ,
    -- fracción y decimal de la cuotaGanadora. Se fija al liquidar.
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS markets_event_idx ON markets (event_id);
CREATE INDEX IF NOT EXISTS markets_status_idx ON markets (status);

-- -----------------------------------------------------------------------------
-- Selecciones (resultado posible dentro de un mercado)
--
-- "which selection won" se representa con una fila en winner_selection_id.
-- Una apuesta lleva sus selecciones en bet_selections; el cruce de ambas es lo
-- que determina el payout.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS selections (
    id           BIGSERIAL PRIMARY KEY,
    market_id    BIGINT      NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    code         TEXT        NOT NULL,      -- HOME / DRAW / AWAY / OVER / UNDER
    name         TEXT        NOT NULL,
    is_winner    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (market_id, code)
);

CREATE INDEX IF NOT EXISTS selections_market_idx ON selections (market_id);

-- -----------------------------------------------------------------------------
-- Cuotas (libro de precios, versionado)
--
-- Una fila por revisión de precio. Una apuesta se liquida contra la fila que
-- vio al colocarse, nunca contra la última. Por eso el índice incluye
-- market_id + created_at y no solo market_id.
--
-- back_price / lay_price: cuota a la que se compra (BACK) y cuota a la que se
-- ofrece el otro lado (LAY). En un exchange hay dos lados por selección.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS odds (
    id           BIGSERIAL PRIMARY KEY,
    market_id    BIGINT      NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    selection_id BIGINT      NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    back_price   NUMERIC(8,3) NOT NULL CHECK (back_price > 1),
    back_size    NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (back_size >= 0),
    lay_price    NUMERIC(8,3) NOT NULL CHECK (lay_price > 1),
    lay_size     NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (lay_size >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS odds_market_created_idx ON odds (market_id, created_at DESC);
CREATE INDEX IF NOT EXISTS odds_selection_created_idx ON odds (selection_id, created_at DESC);

-- -----------------------------------------------------------------------------
-- Wallets
--
-- available = efectivo gastable. exposure = pasivo potencial (lo que perdería
-- si la apuesta saliera mal). En un exchange un LAY puede tener un pasivo
-- mucho mayor que el stake: es por eso que available y exposure se llevan
-- por separado en vez de un único "saldo".
--
-- CHECK available >= 0: imposible quedar en negativo en un exchange. Un
-- LAY que no se puede permitir simplemente se rechaza.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wallets (
    user_id      BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    currency     CHAR(3)    NOT NULL DEFAULT 'EUR',
    available    NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (available >= 0),
    exposure     NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (exposure >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Apuestas
--
-- status:
--   PLACED   -> aceptada, pendiente de emparejar
--   MATCHED  -> emparejada total o parcialmente
--   WON/LOST -> liquidada
--   VOID     -> cancelada (evento abandonado, mercado anulado)
--
-- PLACED es transitorio en un exchange real: se empareja al instante. Aquí se
-- mantiene por los segundos que tarda el emparejador, que es lo que genera
-- el estado "pendiente" observable durante las pruebas.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bets (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT      NOT NULL REFERENCES users(id),
    market_id      BIGINT      NOT NULL REFERENCES markets(id),
    -- Lote. Agrupa operaciones de un mismo instante para poder revertirlas
    -- juntas si algo va mal. Requisito de los exchanges reales.
    bet_group_id   UUID        NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'PLACED'
                               CHECK (status IN ('PLACED','MATCHED','WON','LOST','VOID')),
    -- El odds_id que el usuario vio. Inmutable: define qué preço se liquida.
    odds_id        BIGINT      NOT NULL REFERENCES odds(id),
    side           CHAR(4)     NOT NULL CHECK (side IN ('BACK','LAY')),
    stake          NUMERIC(12,2) NOT NULL CHECK (stake > 0),
    -- payout: lo que entra en el wallet si acierta. Para BACK = stake * price.
    -- Para LAY = stake - stake*(price-1). Guardarlo calculado al colocar evita
    -- depender del precio actual al liquidar.
    potential_payout NUMERIC(12,2) NOT NULL,
    -- payout real tras liquidar (0 si se pierde). Es el "neto" del exchange.
    payout         NUMERIC(12,2),
    -- Sello de idempotencia: evita cobrar dos veces al reintentar una liquidación
    liquidation_key TEXT,
    placed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS bets_user_idx      ON bets (user_id, placed_at DESC);
CREATE INDEX IF NOT EXISTS bets_market_idx    ON bets (market_id, status);
CREATE INDEX IF NOT EXISTS bets_group_idx     ON bets (bet_group_id);
CREATE INDEX IF NOT EXISTS bets_placed_at_idx ON bets (placed_at DESC);
-- Índice parcial: las apuestas sin liquidar son las que se consultan en cada
-- barrido de liquidación, y son una fracción del total. Un índice total sería
-- desperdicio de escritura en la tabla más caliente del sistema.
CREATE INDEX IF NOT EXISTS bets_unsettled_idx ON bets (placed_at)
    WHERE status IN ('PLACED','MATCHED');

-- Idempotencia de la liquidación. Es la garantía que impide pagar dos veces.
--
-- UNIQUE (no solo un índice): la restricción es lo que hace que un reintento
-- concurrente falle en la base de datos en vez de pasar. Con un índice normal no
-- habría conflicto, ambos reintentos insertarían y ambos pagarían.
--
-- Clave NULA en apuestas sin liquidar: en Postgres un índice único admite
-- múltiples NULL (NULL != NULL), así que las apuestas abiertas no colisionan
-- entre sí y solo queda protegida la que ya tiene clave de liquidación.
CREATE UNIQUE INDEX IF NOT EXISTS bets_liquidation_key_uidx
    ON bets (liquidation_key)
    WHERE liquidation_key IS NOT NULL;

-- -----------------------------------------------------------------------------
-- Ledger (append-only)
--
-- Cada cambio de saldo tiene su fila. balance_after es un cache redundante
-- que sirve para detectar divergencias; la verdad es SUM(amount).
--
-- ON DELETE RESTRICT implícito (sin ON DELETE CASCADE): el ledger nunca se
-- borra. Si se pudiera borrar, el saldo deja de ser auditable.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES users(id),
    bet_id        BIGINT      REFERENCES bets(id),
    type          TEXT        NOT NULL
                              CHECK (type IN ('DEPOSIT','WITHDRAW','BET_STAKE','BET_PAYOUT','BET_REFUND','ADJUSTMENT')),
    amount        NUMERIC(14,2) NOT NULL,   -- signed: negativo = sale del wallet
    balance_after NUMERIC(14,2) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS transactions_user_idx ON transactions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS transactions_bet_idx  ON transactions (bet_id);

-- -----------------------------------------------------------------------------
-- Notificaciones (eventos que el usuario "ve")
--
-- Es el servicio no crítico: si se cae, el usuario sigue pudiendo apostar,
-- solo deja de enterarse en tiempo real. Esa independencia es justamente lo
-- que la prueba de tolerancia a fallos tiene que demostrar.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bet_id       BIGINT      REFERENCES bets(id),
    kind         TEXT        NOT NULL
                             CHECK (kind IN ('BET_PLACED','BET_MATCHED','BET_WON','BET_LOST','MARKET_SUSPENDED','MARKET_SETTLED')),
    payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- pending = aún no entregada. El worker de notificaciones la marca
    -- delivered. Sirve para medir lag observable por el usuario.
    delivered    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS notifications_pending_idx ON notifications (created_at)
    WHERE NOT delivered;
