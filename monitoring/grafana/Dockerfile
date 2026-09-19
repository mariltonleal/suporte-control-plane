# Provisionamento (datasource + dashboards) embutido na imagem — mesmo motivo do Prometheus:
# bind mounts relativos nao funcionam em stack Git do Portainer.
FROM grafana/grafana:12.1.1
COPY provisioning /etc/grafana/provisioning
# Fora de /var/lib/grafana: esse caminho e o volume de dados e esconderia os arquivos da imagem.
COPY dashboards /etc/grafana/dashboards
