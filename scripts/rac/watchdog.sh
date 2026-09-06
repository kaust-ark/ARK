#!/bin/bash
# Records status and repairs what can be repaired unattended.
#
# The failure that cost the most here was silent: the orchestrator died, or the
# API refused every call while the Room loop kept burning hops on empty results,
# and nobody noticed for hours. So this both logs and acts — but conservatively,
# because an auto-restarter that loops is worse than a dead run. A restart needs
# a FRESH Room: resume_point() counts hops from the Room log, so restarting into
# the old Room would resume at the cap and do nothing.
cd /home/luoy0a/research/ARK/ARK-runtime/ARK || exit 1
OUT=/tmp/rac_watchdog.log
ARKPY=/home/luoy0a/anaconda3/envs/ark-base/bin/python
MAX_RESTARTS=2
declare -A RESTARTS=( [finance]=0 [scsl]=0 [bialign2]=0 )
declare -A PORT=( [finance]=50451 [scsl]=50452 [bialign2]=50453 )

log() { echo "$(date '+%H:%M:%S') $*" >> "$OUT"; }

fresh_room() {   # $1 = short name; prints the invite, empty on failure
  local n=$1 port=${PORT[$n]} f=/tmp/racwd_invite_${n}.txt
  PORT[$n]=$((port + 10))
  rm -f "$f"
  setsid nohup env PYTHONPATH=. python3 scripts/rac/dev_room.py --port "$port" \
    --room "rom_wd${n}$(date +%s)" --invite-file "$f" \
    > "/tmp/racwd_room_${n}.log" 2>&1 < /dev/null &
  for _ in 1 2 3 4 5 6; do sleep 2; [ -s "$f" ] && { cat "$f"; return; }; done
}

while true; do
  for n in finance scsl bialign2; do
    d=projects/rac_${n}_b
    lf=$(ls -t "$d"/auto_research/logs/*.log 2>/dev/null | head -1)
    alive=$(pgrep -cf "ark.orchestrator --project rac_${n}_b")
    step=$(sed 's/\x1b\[[0-9;]*m//g' "$lf" 2>/dev/null | grep -oE "STEP [0-9]/4: [A-Za-z ]+|\[room\] hop [0-9]+" | tail -1 | tr -d '\n')
    limit=$(grep -c 'usage limits' "$lf" 2>/dev/null | tr -d '\n'); limit=${limit:-0}
    age=$(( $(date +%s) - $(stat -c %Y "$lf" 2>/dev/null || date +%s) ))
    cost=$(sed 's/\x1b\[[0-9;]*m//g' "$lf" 2>/dev/null | grep -oE '💰 \$[0-9.]+' | tr -d '💰 $' | paste -sd+ | bc 2>/dev/null)
    pdf=$([ -f "$d/paper/main.pdf" ] && echo yes || echo no)
    log "$(printf '%-9s proc=%s %-26s limit=%-3s stale=%-5ss cost=$%-7s pdf=%s' \
         "$n" "$alive" "${step:-init}" "$limit" "$age" "${cost:-0}" "$pdf")"

    # The API refusing every call: a restart cannot help, and the Room loop would
    # burn its whole hop budget on empty results. Stop it instead.
    if [ "$limit" -gt 5 ] && [ "$alive" -gt 0 ]; then
      log "!! $n: API 限额已撞 ${limit} 次 — 停掉，避免空转烧完跳数"
      pkill -9 -f "ark.orchestrator --project rac_${n}_b"
      pkill -9 -f "conda run.*rac_${n}_b"
      rm -f "$d/.pid"
      continue
    fi

    # Dead before producing a PDF, and not because of the limit: one clean retry.
    if [ "$alive" -eq 0 ] && [ "$pdf" = "no" ] && [ "$limit" -le 5 ]; then
      if [ "${RESTARTS[$n]}" -lt "$MAX_RESTARTS" ]; then
        RESTARTS[$n]=$(( RESTARTS[$n] + 1 ))
        log "!! $n: 进程已死且无 PDF — 第 ${RESTARTS[$n]} 次重启（换新 Room）"
        pkill -9 -f "conda run.*rac_${n}_b"; rm -f "$d/.pid"
        inv=$(fresh_room "$n")
        if [ -n "$inv" ]; then
          "$ARKPY" - "$d" "$inv" <<'PY'
import sys, pathlib, yaml
root, invite = pathlib.Path(sys.argv[1]), sys.argv[2]
c = yaml.safe_load((root / "config.yaml").read_text())
c["sharednet"]["invite"] = invite
(root / "config.yaml").write_text(
    yaml.dump(c, default_flow_style=False, allow_unicode=True, sort_keys=False))
PY
          PYTHONPATH=. "$ARKPY" -m ark.cli run "rac_${n}_b" --iterations 2 --max-days 1 >> "$OUT" 2>&1
        else
          log "!! $n: 新 Room 起不来，放弃自动重启"
        fi
      else
        log "!! $n: 已重启 ${MAX_RESTARTS} 次仍失败 — 需要人工介入"
      fi
    fi
  done

  bal=$(python3 -c "
import yaml, json, urllib.request
k = yaml.safe_load(open('.ark/config.yaml'))['openrouter_api_key']
d = json.loads(urllib.request.urlopen(urllib.request.Request(
    'https://openrouter.ai/api/v1/credits',
    headers={'authorization': 'Bearer ' + k}), timeout=20).read())['data']
print(f\"{d['total_credits'] - d['total_usage']:.2f}\")" 2>/dev/null)
  log "OpenRouter余额 \$${bal:-?}"
  awk -v b="${bal:-99}" 'BEGIN { if (b + 0 < 8) print "!! OpenRouter 余额低于 $8 — 概念图/judge 可能失败" }' >> "$OUT"
  echo "---" >> "$OUT"
  sleep 300
done
