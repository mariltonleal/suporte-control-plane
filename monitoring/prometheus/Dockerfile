# Config embutida na imagem: em stack Git do Portainer o clone fica dentro do container do Portainer,
# entao bind mounts relativos (./monitoring/...) nao existem no host e o container nem sobe.
FROM prom/prometheus:v3.6.0
COPY prometheus.yml /etc/prometheus/prometheus.yml
