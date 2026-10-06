FROM nginx:1.27-alpine
# default.conf: single user. multi.conf: one app server per Google account (docker-compose.multi.yml).
ARG NGINX_CONF=default.conf
COPY ${NGINX_CONF} /etc/nginx/conf.d/default.conf
