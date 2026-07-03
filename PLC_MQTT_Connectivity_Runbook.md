# MQTT Broker ↔ PLC Connectivity — Runbook
**The Castle Fun Center · weather-mqtt controller**
_Last updated: 2026-06-30_

---

## 0. Conventions

- Commands shown in a code block are **copy‑paste**. Run them one block at a time and read the output before moving on.
- `# comment` lines explain a command — you don't type them.
- Replace **`<IFACE>`** with your PC's real network interface name (you'll find it in Step B2). On the test laptop so far it has been **`eno1`**.
- "The laptop" = the **Linux PC on the `.12` VLAN** you'll use as the temporary test broker.
- "The server" = the Ubuntu VM at **192.168.1.104** running the real broker + controller.
- "The PLC" = the AutomationDirect Productivity unit at **192.168.12.10**.

---

## 1. Where things stand

**Working ✅**
- Server (Ubuntu VM on Hyper‑V) at **192.168.1.104**.
- Mosquitto broker listening on **`0.0.0.0:1883`**, **anonymous** (no auth) — answers on every server IP.
- `weather-mqtt` controller running and connected to the broker via `127.0.0.1`.
- Dashboard live; the rule resolves to **ALLOW** (the precip null→0 fix is in).
- Retained directive present on topic **`irrigation/rain_inhibit`** (`ALLOW` / `INHIBIT`).
- **Proven:** a laptop on the PLC's VLAN (`.12`) pinged the broker, opened TCP 1883, and subscribed — it pulled `ALLOW`. The network path `.12 → broker` works.

**Not working ❌**
- The PLC (192.168.12.10) returns **error 101** ("network unreachable"); `RPIMQTT.Connected` stays false.

**Key facts**
- VLANs: `.1` = server, `.12` = PLC, `.8` = sandbox/test. A router routes between them.
- PLC IP config (confirmed correct): IP `192.168.12.10`, mask `255.255.255.0`, gateway `192.168.12.1`, DNS `1.1.1.1`, DHCP off (static).
- The `.12` laptop confirmed `.12` has a working gateway (`192.168.12.1`) + DHCP and full reach to the broker.

---

## 2. The plan — cheapest test first

1. **Step A** — re-confirm a `.12` PC can reach the real broker.
2. **Step B** — stand up a **test broker on the `.12` laptop** and connect the PLC to it (same subnet, zero routing). **This is the decisive test.**
3. **Decision** — Step B says whether the fix is on the **network** or on the **PLC**.
4. **Step C** — only if B says "network": put the server on `.12` (keep `.1.104`).

> ⚠️ **Do not skip Step B.** If the PLC can't connect to a broker on its *own* subnet, moving the server to `.12` will not help — the problem would be the PLC itself, and Step C would be wasted effort.

---

## Step A — Confirm a PC on .12 can reach the real broker
_(Already passed once. Repeat only if something changed. If you're confident, skip to Step B.)_

**A1.** Put the PC on the `.12` VLAN — plug it into a switch port set to **access VLAN 12**, then renew DHCP (full procedure in **B2** below).

**A2.** Confirm you're on `.12` with a gateway:
```bash
ip -br a            # your interface should show 192.168.12.x
ip route            # MUST contain a line: default via 192.168.12.1
```

**A3.** Reach the broker host and port:
```bash
ping -c3 192.168.1.104
nc -vz 192.168.1.104 1883
mosquitto_sub -h 192.168.1.104 -t 'irrigation/rain_inhibit' -v -W 3
```
**Pass =** ping replies, `nc` prints **"succeeded"**, and `mosquitto_sub` prints **`irrigation/rain_inhibit ALLOW`**.
_(Last result: PASS — `192.168.12.75` reached the broker and got `ALLOW`.)_

→ Network path is good. Continue to **Step B**.

---

## Step B — Test broker on the .12 laptop (the decisive test)

You'll run a throwaway Mosquitto broker **on the laptop**, point the **PLC** at the laptop's `.12` address, and watch whether the PLC connects. Same subnet → no routing, no gateway, no firewall-between-VLANs — so this isolates "is it the PLC, or the cross‑VLAN path?"

### B1 — Make sure the laptop is physically on the .12 VLAN
The laptop's network cable must be in a switch port set to **access VLAN 12** (untagged). This is the same port type the laptop used to get `192.168.12.75`.

### B2 — Identify the interface and force a .12 DHCP lease
```bash
ip -br a
```
Read the output — you want the **wired** line that is `UP`. Example from this build:
```
lo        UNKNOWN   127.0.0.1/8 ::1/128
enp4s0    DOWN
eno1      UP        192.168.12.75/24 ...
wlp2s0    DOWN
```
Here the live interface is **`eno1`**. Use that as **`<IFACE>`** below.

If the IP is **not** `192.168.12.x` yet (e.g. it shows a `.8.x` or `.1.x` address), it's holding an old lease — renew it:
```bash
# Preferred (NetworkManager):
sudo nmcli device disconnect eno1 && sudo nmcli device connect eno1

# If that isn't available, release/renew directly:
sudo dhclient -r eno1 && sudo dhclient -v eno1

# Last resort, bounce the link:
sudo ip link set eno1 down && sudo ip link set eno1 up
```
Re-check:
```bash
ip -br a            # eno1 should now read 192.168.12.x
ip route            # default via 192.168.12.1
```

**If it still won't pull a .12 address:** the port isn't really access‑VLAN‑12, or there's no DHCP on `.12`. You can set a temporary static address to keep going (gone on reboot):
```bash
sudo ip addr add 192.168.12.75/24 dev eno1          # pick any free .12 address
sudo ip route add default via 192.168.12.1 2>/dev/null || true
ip -br a ; ip route
```

### B3 — Note the laptop's .12 address (you'll give this to the PLC)
```bash
ip -br a | awk '/192\.168\.12\./ {print $1, $3}'
```
Example output: `eno1   192.168.12.75/24` → the laptop's broker address for the PLC is **`192.168.12.75`**.

### B4 — Install Mosquitto (broker + client tools)
```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients
```
Confirm it installed and see the version (2.0+ defaults to localhost‑only — that's why B5 is required):
```bash
mosquitto -h 2>&1 | head -1        # e.g. "mosquitto version 2.0.18"
```

### B5 — Configure the test broker to listen on the LAN, anonymously
By default Mosquitto 2.x listens only on `127.0.0.1`. Add a drop‑in config so the PLC can reach it:
```bash
printf 'listener 1883 0.0.0.0\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/test.conf
```
Expected output (it echoes what it wrote):
```
listener 1883 0.0.0.0
allow_anonymous true
```

### B6 — Start the broker and confirm it's listening on all interfaces
```bash
sudo systemctl restart mosquitto
sudo systemctl status mosquitto --no-pager | head -5      # want: "active (running)"
sudo ss -tlnp | grep 1883
```
**`ss` must show `0.0.0.0:1883`** (not `127.0.0.1:1883`):
```
LISTEN 0  100  0.0.0.0:1883  0.0.0.0:*  users:(("mosquitto",pid=...))
```
If it's still `127.0.0.1:1883`, the config didn't load — re-check B5 and restart. If the service won't start, read why:
```bash
sudo journalctl -u mosquitto -n 30 --no-pager
```

### B7 — Open the laptop's own firewall for 1883 (common gotcha)
The laptop may have a host firewall blocking inbound connections even though the broker is listening:
```bash
sudo ufw status
```
- If it says **`Status: inactive`** → nothing to do.
- If it's **active**, allow the port:
  ```bash
  sudo ufw allow 1883/tcp
  sudo ufw status | grep 1883
  ```

### B8 — Seed a retained directive and verify the broker locally
```bash
# put a retained ALLOW on the topic so the PLC gets a value immediately on connect:
mosquitto_pub -h 127.0.0.1 -t irrigation/rain_inhibit -m ALLOW -r

# read it back locally (should print, then exit after 2s):
mosquitto_sub -h 127.0.0.1 -t irrigation/rain_inhibit -v -W 2
```
Expected:
```
irrigation/rain_inhibit ALLOW
```
Now confirm it also answers on the **`.12` address** (what the PLC will use), not just loopback:
```bash
mosquitto_sub -h 192.168.12.75 -t irrigation/rain_inhibit -v -W 2     # use YOUR .12 IP from B3
```
Same `ALLOW` output = the broker is reachable on the LAN address.

### B9 — Start watching for the PLC (leave this running)
Open the broker log so you can SEE the PLC connect in real time:
```bash
sudo tail -f /var/log/mosquitto/mosquitto.log
#   if that file doesn't exist on your build, use the journal instead:
#   sudo journalctl -u mosquitto -f
```
Leave that running. _(Optional: in a second terminal, watch all traffic: `mosquitto_sub -h 127.0.0.1 -t '#' -v`.)_

### B10 — Point the PLC at the laptop broker
On the PLC, in **MQTT Client Properties**:
- **Use IP Address = `192.168.12.75`** ← the laptop's `.12` address from **B3**
- **TCP Port = `1883`**
- **Use Authentication = unchecked**
- **No Encryption (Standard MQTT)**
- Subscriber 1 → Broker Topic `irrigation/rain_inhibit`, QoS `1`, payload → a **String** tag (`RainInhibit_MSG`)
- Save, then **re-transfer the project / cycle the MQTT Enable tag** so it re-attempts.

### B11 — Read the result
Watch the `tail -f` window from B9:

- ✅ **PLC connects** — you'll see lines like:
  ```
  New connection from 192.168.12.10:xxxxx on port 1883.
  New client connected from 192.168.12.10 as RPIMQTT (p2, c1, k30).
  ```
  And on the PLC, `RPIMQTT.Connected` = **true**, `RainInhibit_MSG` = **`ALLOW`**.
  → **The PLC is fine.** The problem is the **cross‑VLAN path for the PLC specifically** → go to **Step C** (put the server on `.12`).

- ❌ **Nothing in the log / PLC still errors** — the PLC can't even reach a broker on its own subnet:
  → It's the **PLC's MQTT config or state**, NOT the network → work the **Section 4** checklist. (Server‑on‑`.12` would not help.)

### B12 — (Optional) Prove the PLC reacts to directives
With the PLC connected, flip the value and watch `RainInhibit_MSG` change on the PLC:
```bash
mosquitto_pub -h 127.0.0.1 -t irrigation/rain_inhibit -m INHIBIT -r
mosquitto_pub -h 127.0.0.1 -t irrigation/rain_inhibit -m ALLOW   -r
```

### B13 — Tear down the test broker (IMPORTANT — do this when finished)
Leaving a second broker running will confuse things later. Remove it:
```bash
sudo rm /etc/mosquitto/conf.d/test.conf
sudo systemctl stop mosquitto
sudo systemctl disable mosquitto      # so it doesn't auto-start on reboot
sudo ss -tlnp | grep 1883 || echo "no broker listening — clean"
```
If you set a temporary static IP in B2, drop it (or just reboot the laptop):
```bash
sudo ip addr del 192.168.12.75/24 dev eno1 2>/dev/null || true
```

---

## Step C — Put the server on the .12 VLAN (keep .1.104)
_Only if Step B showed the PLC connects fine to a same‑subnet broker._

**Goal:** give the Hyper‑V Ubuntu VM a second IP on `.12` (e.g. `192.168.12.5`) alongside `192.168.1.104`. The real broker already listens on `0.0.0.0`, so **no broker change is needed** — it answers on the new IP automatically. Then point the PLC at `192.168.12.5` (same subnet → no routing → no 101).

### C1 — Prerequisite (the hard part): Hyper‑V must deliver VLAN 12 to the VM

> The Hyper‑V **host's uplink switch port must carry VLAN 12**. If that port is access/VLAN‑1 only, none of this works — get it trunked first, or use a spare host NIC cabled to a VLAN‑12 access port + a second External vSwitch. (This was the blocker on the first attempt.)

**Option 1 — Hyper‑V handles the tag (recommended, GUI):**
1. Hyper‑V Manager → the VM → **Settings → Add Hardware → Network Adapter** (connect it to the External vSwitch).
2. Select that adapter → **☑ Enable virtual LAN identification → VLAN ID `12`**.

**Option 2 — guest tags (PowerShell on the Hyper‑V host):**
```powershell
Get-VM                       # get the exact VM name
Set-VMNetworkAdapterVlan -VMName "<VMName>" -Trunk -AllowedVlanIdList 12 -NativeVlanId 1
Get-VMNetworkAdapterVlan  -VMName "<VMName>"   # verify: Trunk / Allowed 12 / Native 1
```

### C2 — Configure the IP inside Ubuntu — `/etc/netplan/00-installer-config.yaml`

**If Option 1 (new adapter shows up as e.g. `eth1`):**
```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
    eth1:
      addresses: [192.168.12.5/24]
      # NO gateway here — keep the single default via 192.168.1.1
```

**If Option 2 (guest VLAN sub-interface):**
```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
  vlans:
    eth0.12:
      id: 12
      link: eth0
      addresses: [192.168.12.5/24]
```

Apply it **safely** (auto-reverts in ~120 s if your SSH drops, so you can't lock yourself out):
```bash
sudo netplan try        # press Enter to keep it if the connection holds
```

### C3 — Verify the server is really on .12
```bash
ip -br a            # the new interface shows 192.168.12.5
ip route            # default STILL via 192.168.1.1 — only ONE default route
sudo ss -tlnp | grep 1883      # still 0.0.0.0:1883
ping -c3 192.168.12.10         # <-- MUST succeed: proves VLAN 12 actually reaches the PLC
```
If the ping to the PLC **fails** (e.g. `Destination Host Unreachable from 192.168.12.5`), the switch/Hyper‑V isn't delivering VLAN 12 to the VM — stop and fix the trunk/VLAN (the server config is fine; it's an upstream issue). Re-check C1.

### C4 — Point the PLC at the server's .12 address
PLC MQTT dialog → **Use IP Address = `192.168.12.5`**, port `1883`. Same subnet as the PLC → no routing → no 101. `RPIMQTT.Connected` should go true and `RainInhibit_MSG` = `ALLOW`.

---

## 3. Decision tree

```
PLC error 101
   │
   ├─ Step A: .12 PC → REAL broker works? (ping + nc 1883 + mosquitto_sub = ALLOW)
   │      └─ NO  → network/firewall between .12 and .1, or no .12 gateway. Fix the network.
   │      └─ YES ↓
   │
   ├─ Step B: PLC → TEST broker on the SAME subnet (laptop) connects? (broker log shows RPIMQTT)
   │      └─ NO  → PLC config/state problem → Section 4 checklist. (Server-on-.12 won't help.)
   │      └─ YES → cross-VLAN path is the issue → Step C (server on .12).
```

---

## 4. PLC-side checklist (if Step B points at the PLC)

- MQTT dialog **Use IP Address** = the broker IP. **Re-open the dialog after saving to confirm it actually stuck** — it started life as `192.168.6.102`; make sure it's the intended `192.168.1.104` (or `192.168.12.75` during the Step B test, or `192.168.12.5` after Step C).
- Port `1883`, **Use Authentication unchecked**, **No Encryption (Standard MQTT)**.
- Subscriber 1: topic `irrigation/rain_inhibit`, QoS `1`, payload → a **String** tag (`RainInhibit_MSG`).
- **Re-transfer the project / power-cycle** the PLC so network + dialog changes take effect.
- After a fresh attempt, note the exact **`RPIMQTT.ErrorCode`** value.

---

## 5. Reference

**Key addresses**

| Thing | Value |
|---|---|
| Server / real broker (VLAN .1) | `192.168.1.104` |
| Broker port | `1883` (anonymous, no TLS) |
| PLC (VLAN .12) | `192.168.12.10` · mask `255.255.255.0` · gw `192.168.12.1` |
| Test laptop / test broker (.12) | `192.168.12.75` |
| Proposed server .12 IP (Step C) | `192.168.12.5` |
| MQTT topic | `irrigation/rain_inhibit` |
| Payloads (retained) | `INHIBIT` (hold watering) / `ALLOW` (water ok) |
| PLC Client ID | `RPIMQTT` |

**Real server broker config** — `/etc/mosquitto/conf.d/weather-mqtt.conf`:
```
listener 1883 0.0.0.0
allow_anonymous true
```
Controller `config.yaml` → `mqtt.host: "127.0.0.1"`.

**Handy commands**
```bash
# (any host) what's listening on 1883 + which pid
sudo ss -tlnp | grep 1883
# (any host w/ clients) watch a broker's live directive
mosquitto_sub -h <broker-ip> -t 'irrigation/rain_inhibit' -v
# (any host w/ clients) force a retained value to test the PLC reacts
mosquitto_pub -h <broker-ip> -t 'irrigation/rain_inhibit' -m INHIBIT -r
mosquitto_pub -h <broker-ip> -t 'irrigation/rain_inhibit' -m ALLOW   -r
# tail a broker's log
sudo tail -f /var/log/mosquitto/mosquitto.log   # or: sudo journalctl -u mosquitto -f
```

**PLC reaction logic (target)**
```
IF RainInhibit_MSG = "INHIBIT" → Irrigation_Permit = OFF   (hold valves closed)
IF RainInhibit_MSG = "ALLOW"   → Irrigation_Permit = ON    (watering allowed)
```
Initialize `RainInhibit_MSG = "INHIBIT"` so it fails safe (no watering) before the first message arrives.

**Rollback — remove the .12 presence from the server (after Step C)**
- Option 1: remove the added vNIC in Hyper‑V Settings, and delete the `eth1:` block from netplan → `sudo netplan apply`.
- Option 2: delete the `vlans:` block from netplan → `sudo netplan apply`; if `eth0.12` lingers, `sudo ip link delete eth0.12`.

---

## 6. One-line summary

Server + broker are done and verified; a `.12` laptop reaches the broker and gets `ALLOW`, so the network path works. **Tomorrow:** run **Step B** (PLC → test broker on the laptop, same subnet). That one test decides whether you put the server on `.12` (**Step C**) or fix the PLC dialog/state (**Section 4**) — so you won't redo the Hyper‑V VLAN work unless it's actually the answer.
