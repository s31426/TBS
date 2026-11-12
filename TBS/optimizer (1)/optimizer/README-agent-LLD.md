# Linux Optimizer Monitoring — agent-side LLD

## Files

- `zabbix_linux_optimizer_monitoring_7.4_IMPORTABLE.yaml` — Zabbix 7.4 template
- `monitoring_generator.py` — generator of `monitoring.json.example`
- `optimizer_lld.py` — agent-side LLD provider
- `optimizer_lld.conf` — Zabbix agent2 `UserParameter`

## Wymagany template na hoście

Host musi mieć podlinkowany **Linux by Zabbix agent active**. Wszystkie triggery optimizera zależą od:

`Zabbix agent is not available`  
`nodata(/Linux by Zabbix agent active/agent.ping,{$AGENT.NODATA_TIMEOUT})=1`

Import dopasowuje zależność po nazwie i expression. W tej instalacji trigger nie ma prefiksu `Linux:`.

**Zabbix agent active** nie może być podlinkowany równolegle — oba szablony mają klucz `agent.ping`.

## Install on monitored host

1. Put `optimizer_lld.py` at `/etc/zabbix/optimizer_lld.py` and `chmod +x`.
2. Put `optimizer_lld.conf` in `/etc/zabbix/zabbix_agent2.d/`.
3. Production config: `/etc/zabbix/monitoring.json`
4. Reload agent2.
5. Test:
   ```bash
   sudo zabbix_agent2 -t 'optimizer.lld[instances]'
   sudo zabbix_agent2 -t 'optimizer.lld[groups]'
   sudo zabbix_agent2 -t 'optimizer.lld[ports]'
   sudo zabbix_agent2 -t 'optimizer.lld[received]'
   sudo zabbix_agent2 -t 'optimizer.lld[received_group]'
   sudo zabbix_agent2 -t 'local.ping[Modbus Sensor 1,192.168.0.22]'
   sudo zabbix_agent2 -t 'net.tcp.service[tcp,192.168.0.22,502]'
   ```

## Template discovery keys (4 rules, no LLD overrides)

| Rule | Key | Creates |
|------|-----|---------|
| instances | `optimizer.lld[instances]` | sent |
| groups | `optimizer.lld[groups]` | ping, device_query |
| ports | `optimizer.lld[ports]` | TCP port |
| received | `optimizer.lld[received]` | received per seria (`mode=each`) |
| received_group | `optimizer.lld[received_group]` | received na grupę (`mode=group`) |

Unified dump for debugging: `optimizer.lld[monitoring]`.

Ping uses existing host UserParameter: `local.ping[{#GROUP_NAME},{#IP}]` (`fping` on `$2`).

## monitoring.json

Pełny opis pól (czysty tekst): `monitoring.json.README.txt`

Dozwolony JSONC (komentarze `//` i `/* */`) — `optimizer_lld.py` je usuwa przed parse.


