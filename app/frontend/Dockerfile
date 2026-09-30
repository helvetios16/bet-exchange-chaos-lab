# =============================================================================
# Dockerfile del frontend — nginx sobre alpine, non-root, read-only
# =============================================================================
FROM nginx:1.27-alpine AS runtime

# --- Sistema de archivos de solo lectura ---
# El pod declara readOnlyRootFilesystem: true, asi que todo lo que nginx
# quiera escribir tiene que estar montado como emptyDir en el manifiesto:
#   /var/cache/nginx   -> cache de proxy
#   /var/run           -> pid y sockets
#   /tmp               -> buffers temporales
RUN rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /var/cache/nginx /var/run /tmp \
 && chown -R 10001:10001 /var/cache/nginx /var/run /tmp \
 && touch /var/run/nginx.pid \
 && chown 10001:10001 /var/run/nginx.pid

# --- Config ---
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html

# --- Usuario no root ---
# El nginx oficial corre como root para poder hacer bind en puertos <1024.
# Aqui se escucha en 8080, que no lo requiere, asi que se puede droppear
# a un uid normal sin perder nada.
RUN addgroup -g 10001 -S kuber \
 && adduser -u 10001 -S -G kuber -H -s /sbin/nologin kuber \
 && chown -R 10001:10001 /usr/share/nginx /etc/nginx

USER 10001:10001

EXPOSE 8080

# Nota: el HEALTHCHECK de la imagen oficial apunta al 80, que no se usa.
# En Kubernetes mandan las startup/readiness/liveness probes del manifiesto,
# asi que no es necesario redefinirlo aqui.

# exec form: PID 1 es nginx y recibe SIGTERM -> graceful shutdown
# (nginx cierra las conexiones abiertas antes de salir). Con la forma shell
# de por defecto, el signal no llegaria y el pod tardaria terminationGracePeriod
# en morir, durante el cual sigue recibiendo traffic que ya no puede servir.
CMD ["nginx", "-g", "daemon off;"]
