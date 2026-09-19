#!/usr/bin/env bash
# Amostrador de recursos para rodar NA VPS medida enquanto o gerador de carga roda em OUTRA maquina.
# Grava um CSV com host (CPU/RAM/rede), containers da stack (CPU/RAM), conexoes do Postgres e do Redis,
# metricas da aplicacao (/api/ops/metrics) e latencia do /api/health.
#
# uso: METRICS_TOKEN=... vps_sampler.sh <stack> <app_url> <segundos> [intervalo=10] [saida.csv]
# ex.: METRICS_TOKEN=$(grep ^METRICS_TOKEN= /root/.secrets/suporte-control-plane.env | cut -d= -f2) \
#      tools/loadtest/vps_sampler.sh sr-empresa-teste https://empresa-teste.suporte.krato.ai 200 10 /tmp/carga-10.csv
set -u
STACK=$1; APP_URL=$2; DUR=${3:-180}; INT=${4:-10}; OUT=${5:-/tmp/sampler-$STACK.csv}
IFACE=$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')

cpu_snapshot() { awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print idle, total}' /proc/stat; }
net_snapshot() { echo "$(cat /sys/class/net/$IFACE/statistics/rx_bytes) $(cat /sys/class/net/$IFACE/statistics/tx_bytes)"; }
num() { echo "${1:-0}" | tr -d '%MiBGgKk ' ; }
mem_mb() { # "55.6MiB / 768MiB" -> MB
  local v=${1%% *}
  case $v in *GiB) echo "$(echo ${v%GiB} | awk '{printf "%.1f",$1*1024}')";; *MiB) echo "${v%MiB}";; *KiB) echo "$(echo ${v%KiB} | awk '{printf "%.1f",$1/1024}')";; *) echo 0;; esac; }

echo "ts,host_cpu_pct,host_mem_used_mb,host_rx_mbps,host_tx_mbps,app_cpu_pct,app_mem_mb,db_cpu_pct,db_mem_mb,redis_cpu_pct,redis_mem_mb,pg_conns,redis_clients,ws_peers,active_sessions,rooms,http_5xx,restarts,health_ms" > "$OUT"
read pidle ptotal < <(cpu_snapshot); read prx ptx < <(net_snapshot); pts=$(date +%s.%N)
END=$(( $(date +%s) + DUR ))
while [ "$(date +%s)" -lt "$END" ]; do
  sleep "$INT"
  read idle total < <(cpu_snapshot); read rx tx < <(net_snapshot); ts=$(date +%s.%N)
  dt=$(echo "$ts - $pts" | bc -l)
  host_cpu=$(echo "scale=1; (1 - ($idle - $pidle) / ($total - $ptotal)) * 100" | bc -l)
  rx_mbps=$(echo "scale=2; ($rx - $prx) * 8 / $dt / 1000000" | bc -l); tx_mbps=$(echo "scale=2; ($tx - $ptx) * 8 / $dt / 1000000" | bc -l)
  pidle=$idle; ptotal=$total; prx=$rx; ptx=$tx; pts=$ts
  host_mem=$(free -m | awk '/^Mem:/{print $3}')
  app_cpu=0; app_mem=0; db_cpu=0; db_mem=0; rd_cpu=0; rd_mem=0
  while IFS='|' read -r name cpu mem; do
    case $name in
      ${STACK}-app-*)   app_cpu=$(num "$cpu"); app_mem=$(mem_mb "$mem");;
      ${STACK}-db-*)    db_cpu=$(num "$cpu");  db_mem=$(mem_mb "$mem");;
      ${STACK}-redis-*) rd_cpu=$(num "$cpu");  rd_mem=$(mem_mb "$mem");;
    esac
  done < <(docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}' $(docker ps -q --filter "label=com.docker.compose.project=$STACK") 2>/dev/null)
  pg=$(docker exec "${STACK}-db-1" psql -U support -d support -tAc "select count(*) from pg_stat_activity where datname='support'" 2>/dev/null || echo 0)
  rc=$(docker exec "${STACK}-redis-1" sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" info clients' 2>/dev/null | awk -F: '/^connected_clients/{gsub("\r","",$2); print $2}')
  restarts=0; for c in $(docker ps -aq --filter "label=com.docker.compose.project=$STACK"); do r=$(docker inspect -f '{{.RestartCount}}' "$c"); restarts=$((restarts + r)); done
  m=$(curl -s -m 5 -H "Authorization: Bearer ${METRICS_TOKEN:-}" "$APP_URL/api/ops/metrics")
  ws=$(echo "$m" | awk '/^support_websocket_peers /{print $2}'); act=$(echo "$m" | awk '/^support_active_sessions /{print $2}')
  rooms=$(echo "$m" | awk '/^support_rooms_loaded /{print $2}'); e5=$(echo "$m" | awk '/^support_http_5xx_total /{print $2}')
  health=$(curl -s -m 5 -o /dev/null -w '%{time_total}' "$APP_URL/api/health" | awk '{printf "%.0f",$1*1000}')
  echo "$(date -u +%H:%M:%S),$host_cpu,$host_mem,$rx_mbps,$tx_mbps,$app_cpu,$app_mem,$db_cpu,$db_mem,$rd_cpu,$rd_mem,${pg:-0},${rc:-0},${ws:-0},${act:-0},${rooms:-0},${e5:-0},$restarts,$health" >> "$OUT"
done
echo "amostras gravadas em $OUT"
