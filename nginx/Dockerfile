FROM nginx:1.27-alpine
# default.conf: single user (docker-compose.single.yml). multi.conf: one app server per Google account (docker-compose.yml).
ARG NGINX_CONF=default.conf
COPY ${NGINX_CONF} /etc/nginx/conf.d/default.conf
