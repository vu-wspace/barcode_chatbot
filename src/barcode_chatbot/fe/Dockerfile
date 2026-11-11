FROM nginx:1.27-alpine
# Rendered by the nginx image at start (envsubst of BE_URL, AI_URL)
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY index.html core.js /usr/share/nginx/html/
