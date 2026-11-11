# stable line (1.27 mainline no longer gets fixes)
FROM nginx:1.28-alpine
# Rendered by the nginx image at start (envsubst of BE_URL, AI_URL)
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY index.html core.js /usr/share/nginx/html/
