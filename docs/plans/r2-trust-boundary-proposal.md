# R2: trust boundary for proxy headers — proposal (variant 2)

The public copy omits internal acceptance reports and links to them; this proposal is not evidence of the current deployment.

Date: 2026-10-04 (variant 1: 2026-10-03), working branch `clean_init`.
**Audit note (SHA c8fe0543): the status below is superseded.** Variant 2 is implemented in this tree: `docker-compose.yml` has the networks `tunnel` and
`tunnel_egress`, the config `tunnel_trust`, and requires `EDU_AI_TUNNEL_SUBNET`; the real tool is `scripts/tunnel_subnet.py` (the file
`tests/nginx_r2/tunnel_subnet.py` is only a stand-in for the test). The section "1. The problem as it is today" describes the state before R2. Whether R2 is deployed on a
specific host cannot be verified from the repository.
**Status: the corrected variant 2 is lab-verified and the reviews R2-R1–R3 are closed within the documented scope. It is a basis for preparing an implementation candidate; it is not implemented or deployed.**
**Follow-up Codex review 2026-10-04:** R2-R3 confirmed, own selfcheck 38/38,
three faulty variants independently detected; the 105 PASS taken over and nine hashes checked. Results after the fix.
**Implementation candidate:** on `clean_init` there are the tools (`796f060085`) and three step commits (`92bb1ac866`, `6d65862de0`, `4a8aff1920`), derived byte for byte from this proposal; checkpoint,
rehearsal results. The candidate is ready for review, **not for deployment**; the production `docker-compose.yml`, `nginx/conf` and applications did not change.
**Codex review (2026-10-03):** returned the original variant for rework of the addressing and the safety of cleanup;
R2-R1 and R2-R2. The number of PASS is not an approval of any variant for implementation or deployment.
Basis: the R2 section in b04a-review.md. Harness: [tests/nginx_r2](../../tests/nginx_r2/README.md).
Diff of the proposal (generated from git, not hand-written): r2-proposed-diff.md.
**Valid results:** r2-lab-results-v3.md (variant 2 after the R2-R3 fix). The records of variant 2 before the fix (97 PASS) and of variant 1
(r2-lab-results.md, JSON) remain unchanged as historical; variant 1 concerns the fixed subnet `192.0.2.0/29`, which is no longer the proposal.

## 0. What variant 2 changes (review fix)

| Finding | Fix | Where it is verified |
|---|---|---|
| **R2-R1** fixed `192.0.2.0/29` (RFC 5737 is for documentation, not for local use) and the same CIDR for every installation | No constant in the repository. The CIDR is an **installation setting** `EDU_AI_TUNNEL_SUBNET`, allocated from the private range `10/8` without known blocks and **checked against the real host state** (Docker networks and pools, routes, interface addresses, known cloud/VPN/Kubernetes ranges, ranges given by the administrator). On an overlap the preparation refuses to continue before it creates anything | sections 4.2–4.4; lab `subnet.*`, `static.*`, offline `subnet.*` |
| **R2-R1** two independent constants (Compose, nginx) | **One source:** Compose takes the same variable for the subnet of the `tunnel` network and for the content of the `tunnel_trust` config that nginx loads via `include`; there is no CIDR in `nginx.conf` or any other file. Without the variable the model does not render | `static.single_source.*`, `static.subnet_required`, `*.cidr_agreement` |
| **R2-R1** two installations on one daemon (same CIDR → overlap) | Every installation has its **own** subnet and its own `tunnel`; the trusted network is not shared between installations | `multi.*` (two concurrent installations) |
| **R2-R2** cleanup by part of the name `b04r2_` without verifying ownership | Cleanup requires a specific **run ID**, selects only by labels, verifies labels before removal (for Compose resources the exact project of the run), removes by ID, and anything that cannot be verified or is not its own is **kept and reported**. Other places that removed by a planned name now verify ownership or refuse | section 4.5; offline `cleanup.*` (including reproduction of the original bug), lab `cleanup.*`, `final.nothing_else_changed` |

## 1. The problem as it is today (production configuration, not the S5 proposals)

| Element | Today |
|---|---|
| Who is in the `edge` network | nginx, `edu_ai_api` (downloads URLs given by the user), `edu_ai` (Langflow, web crawl), `edu_ai_search`, `edu_ai_pgadmin`, `edu_ai_web_chat`, `edu_ai_qa_admin` and `cloudflared` |
| Where nginx listens | 80, 8443, 8444 published on the host; **81 only inside containers, but on all nginx interfaces**, hence reachable by every member of `edge` |
| Whom nginx trusts for the client address | on port 81 `set_real_ip_from 172.16.0.0/12` + `real_ip_header CF-Connecting-IP`; on the other ports nobody |
| What is sent to the applications | `X-Real-IP $remote_addr`, `X-Forwarded-For $proxy_add_x_forwarded_for` (**the chain from the caller is kept and nginx appends its own address**), `X-Forwarded-Proto $scheme` (always `http`) |
| What the applications do with it | no code in `api/`, `search/`, `web_chat/` or `qa_portal/` reads `X-Real-IP`, `X-Forwarded-*`, `CF-Connecting-IP` or `request.client`; Uvicorn is started without proxy flags (default `FORWARDED_ALLOW_IPS=127.0.0.1`) |

A private sender address does not prove that it is a trusted proxy: anyone on `edge` (in particular the API, which after abuse downloads foreign URLs)
can send any `CF-Connecting-IP` to `nginx:81` and nginx believes it. The lab **reproduced** this on the production configuration (variants 1 and 2).
Impact today: bypassing the per-IP rate limit (`api_limit`, 10 r/s), exhausting somebody else's bucket (an attack on a specific client), a forged access log
and a faulty boundary for any future code that would believe the headers. There is no direct impact on identity in the applications today, because they do not read it from the headers.

## 2. Four layers (not to be confused in the proposal or in the tests)

| Layer | What it is | Whom to trust |
|---|---|---|
| **TCP sender** | the address from which nginx actually accepted the connection | the only fact the caller cannot choose |
| **received headers** | what the sender wrote into the request (`CF-Connecting-IP`, `X-Forwarded-*`) | nobody by themselves; only a header from a **proven** proxy can be trusted |
| **client address in nginx** (`$remote_addr`, access log, `limit_req`) | the TCP sender, or the value of `CF-Connecting-IP` if the sender belongs to `set_real_ip_from` | what nginx decided based on the previous two layers |
| **what the application sees** | headers from nginx (`X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`) and what Uvicorn makes of them (`scope["client"]`, `scope["scheme"]`) | Uvicorn only the sender from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`, i.e. not nginx) |

A broad private range, a service name or the acceptance of a header are not proof of trustworthiness. The proposal therefore defines a trusted sender
**by membership in a network that only it may belong to**.

## 3. Options considered

| Option | Verdict | Reason |
|---|---|---|
| keep or widen the private CIDR (`172.16.0.0/12`, proposal S5 additionally `192.168/16`, `10/8`) | rejected | every member of `edge` is in the range; a wider range only enlarges the problem |
| a service name in `set_real_ip_from` | rejected | nginx resolves the name only at configuration load (documentation of the `realip` module), not at run time: after a container replacement the address is stale until a reload; an unresolvable name at startup would also prevent nginx from starting, which S2 deliberately prevents |
| trust nobody | rejected | the rate limit on port 81 would become shared by the whole internet (the original reason for the function) |
| fixed IP of cloudflared in `edge` | rejected | requires pinning the subnet and address of the shared `edge` network (reshaping the network of all eight containers) and a proxy replacement would have to take the same address |
| shared secret in a header (Cloudflare Transform Rule) | rejected | the secret and configuration only in the dashboard; anyone who knows it is "the proxy"; more complex than a network boundary |
| UNIX socket (`listen unix:`, `set_real_ip_from unix:;`, ingress `unix:`) | **stronger alternative, not verified** | trust follows from holding the volume, without addresses and subnets (and hence without the whole allocation question from R2-R1). Cloudflared supports a UNIX service in ingress rules, but the documentation does not explicitly confirm the UNIX type for a dashboard-managed tunnel; it requires a change of the public name in the dashboard and the behaviour of a leftover socket after a crash was not tried. It does not belong in this lab |
| cloudflared in the nginx network namespace (`network_mode: service:nginx`) | rejected | couples the life cycles (after an nginx replacement cloudflared stays in a defunct namespace, which S2 removed) and takes away cloudflared's `app` network for Bifrost |
| `FORWARDED_ALLOW_IPS=*` in the applications | rejected | a caller from `edge` could then change identity directly, even without nginx; the lab showed this with a real Uvicorn |
| fixed subnet in the repository (variant 1: `192.0.2.0/29`) | **rejected by review R2-R1** | the range is reserved for documentation, not for local use; the same CIDR for every installation overlaps on one daemon; two independent constants may diverge |
| **a dedicated `tunnel` network, only nginx + cloudflared; the CIDR is an installation setting with a single source, allocated and checked against the host state** | **recommended (variant 2)** | no change in the dashboard, no application changes, small range; fails safe |

## 4. Recommended proposal (variant 2)

### 4.1 Networks and trust boundary

**The `tunnel` network** (`internal: true`, only nginx and cloudflared) and the `tunnel_egress` network (cloudflared → Cloudflare, nobody else). nginx is in `edge` + `tunnel`;
cloudflared in `app` + `tunnel` + `tunnel_egress` (**no longer in `edge`**). The public name in the dashboard stays `http://edu_ai_nginx:81`: cloudflared shares only
`tunnel` with nginx, so the name resolves exactly to nginx's address in this network. `X-Forwarded-For` is sent as `$remote_addr` (seven places), the chain from the caller is dropped.

### 4.2 One source of the CIDR (R2-R1)

```text
EDU_AI_TUNNEL_SUBNET=<installation subnet>   # installation setting; no value or default in the repository
        │
        ├─ docker-compose.yml: networks.tunnel.ipam.config[].subnet: ${EDU_AI_TUNNEL_SUBNET:?...}
        └─ docker-compose.yml: configs.tunnel_trust.content:  set_real_ip_from ${EDU_AI_TUNNEL_SUBNET};
                  └─ mounted into nginx as /etc/edu_ai/tunnel/trust.conf ── nginx.conf (server 81):  include /etc/edu_ai/tunnel/*.conf;
```

* There is **no CIDR** in `nginx.conf` or in any other tracked file; `docker compose config` without the variable **does not render** the model (a default value would hide a constant).
* **Without the file nginx trusts nobody and starts** (a glob with no match is allowed in nginx; verified with `nginx -t`): a safe fall, not an outage.
* Both uses of the variable arise from a single Compose rendering, so they cannot diverge. The lab nevertheless verifies them from the **actually running** system: the setting = the subnet of the Docker network =
  the range nginx runs with (`nginx -T`). Deliberately broken variants (a second constant in `nginx.conf`, a different literal in the config) are caught by the checks.
* A change of the value on an existing installation: as long as a network with a different subnet exists, the check reports `NETWORK_MISMATCH` and the network is not changed (verified). How Compose would rebuild
  the network and `nginx` on such a change was **not verified**; changing the subnet of an existing installation is therefore not part of this proposal and belongs in a separate procedure.
* Requires a Compose that supports `configs` with `content` and interpolation (from 2.23.1); lab 5.3.0, production per earlier read-only checks 5.3.1.

### 4.3 Allocating the subnet to an installation and the preflight (R2-R1)

The tool [tunnel_subnet.py](../../tests/nginx_r2/tunnel_subnet.py) (in the harness; it belongs in the repository of operations scripts only at implementation) is **read-only** and has two commands:
`allocate --project NAME` (prints `EDU_AI_TUNNEL_SUBNET=<value>`) and `check CIDR --project NAME` (a mandatory preflight on every creation or update).
Exit code 0 = free; **2 = refused (collision or invalid value)**; **3 = refused, the host state cannot be verified** (the unverified is not considered free).
**Every mandatory read (routes, addresses, Docker) has its own exit code and the failure of any one is code 3**; the success of one read never masks the failure of another (R2-R3). The helper container therefore
returns 11 on a route read failure and 12 on an address read failure, and a command that `ip` rejects (e.g. `table all`) is never replaced by reading only the `main` table.
An empty output of a successful read is a result; no routes and at the same time no addresses, or a successful read with unreadable output, is refused (code 3).

| What is read (read only) | What is refused |
|---|---|
| `docker network ls/inspect` (all networks of all projects) | an overlap with a network of another project (`DOCKER_NETWORK`); the installation's own network `<project>_tunnel` with the same subnet is not a collision, with a different subnet it is `NETWORK_MISMATCH` and the network is not changed |
| the daemon's address pools (`docker info`, built-in `172.16.0.0/12` and `192.168.0.0/16`) | an overlap with a pool (`DOCKER_POOL`) |
| host routes **from all IPv4 tables** (`ip -4 route show table all`: main, local and policy tables, e.g. VPN) | an overlap with a route in any table (`HOST_ROUTE`); the installation's own bridge (`br-<network id>`, including its local and broadcast routes) is excluded, otherwise a running installation would refuse itself on the check |
| host interface addresses including VPN and WireGuard (`ip -4 addr`) | an overlap with an interface address/network (`HOST_ADDRESS`) |
| a built-in list of known ranges | `DENYLIST`: Docker pools `172.16.0.0/12`, `192.168.0.0/16`; Swarm/Azure/home LAN `10.0.0.0/16`; OpenVPN `10.8.0.0/16`; k3s `10.42.0.0/15`; **the reserved WireGuard example `10.66.0.0/16`**; kubeadm `10.96.0.0/12`; GCP `10.128.0.0/9`; flannel/AKS `10.244.0.0/16`; `100.64.0.0/10`; `169.254.0.0/16` |
| `--reserved CIDR` (administrator) | `RESERVED`: LAN, VPN, VPC and other ranges the host does not see |
| the value itself | `INVALID` (not a canonical network: host bits set, a length other than /28–/29, IPv6) and `NOT_PRIVATE` (outside RFC 1918) |

**Choice of range and its justification.** A small `/29` subnet (6 usable addresses: nginx, cloudflared and its replacement at run time) is allocated from `10.0.0.0/8` **after subtracting the known blocks**.
The ranges `172.16.0.0/12` and `192.168.0.0/16` are not chosen: the Docker pools, home and office LANs and the Docker Desktop VM (`192.168.65.0/24`) lie in them, so they would collide most often. The position
in the pool is derived from the project name (only to spread installations out; it proves nothing) and **usability is decided solely by the preflight against the real host state**.
An unusual number is not proof that the range does not collide, and a range that is free today may be taken tomorrow by a new VPN or route: that is why `check` runs on **every** creation and update and the value
is stored in the installation settings after allocation, not recomputed on every start. Invisible ranges (LAN and VPN the host does not know about) must be given by the administrator via `--reserved`.
When the pool is exhausted the tool refuses (`NoFreeSubnet`) and the decision is up to the owner.

**Production.** In this step the production host was **neither checked nor read**. Collisions with its routes, WireGuard, VPC/LAN and existing Docker networks are therefore **not verified**;
`allocate` and `check` on the production host (with direct reading of `ip`, `--routes host`) are a **mandatory preflight before a later rollout** and their output is recorded.

**No sharing.** Every installation (Compose project) has its own `tunnel` network with its own subnet; the check refuses when another installation requests an already allocated CIDR, so the trusted network is not shared between
installations and the trust boundary of one installation does not concern the other (lab: the nginx of each installation runs with a single range, its own; a connection across the boundary times out).

### 4.4 Impacts

| Area | Impact |
|---|---|
| Networks | +`tunnel`, +`tunnel_egress`; cloudflared leaves `edge` (it can no longer reach services in `edge`, see the assumptions); nginx gets a second interface; new config `tunnel_trust` |
| Installation settings | new variable `EDU_AI_TUNNEL_SUBNET` in the installation settings (on production `.env`; **not changed in this step**); without it the model does not render |
| Listeners | unchanged: 80, 8443, 8444 published the same way, 81 the same as today. A member of `edge` still reaches port 81, but **as itself** (as on port 80); its `CF-Connecting-IP` is ignored |
| Headers | `X-Forwarded-For` = one address (the client from the trusted proxy, otherwise the TCP sender); `X-Real-IP` and `X-Forwarded-Proto` unchanged (nginx always overwrites them, `X-Forwarded-Proto` is `http`). The headers `CF-Connecting-IP`, `Forwarded`, `True-Client-IP` from the caller are still passed on unchanged: applications must not read them |
| Applications | no code change; none reads these headers; Uvicorn does not trust them without `FORWARDED_ALLOW_IPS` (verified in the real versions 0.52.4 and 0.35.0) |
| Rate limit and log | the key is the client from the trusted proxy, otherwise the real TCP sender; spoofing no longer changes either |

### 4.5 Safe lab cleanup (R2-R2)

Cleanup is limited to a single run and ownership is proven by labels, not by name: a resource is its own only when `edu_ai.purpose` = the lab label, `edu_ai.run` = the run ID and for Compose resources
`com.docker.compose.project` equals the project recorded for this run (`edu_ai.project`) and has the exact shape of this run's project. `cleanup --run-id ID` selects candidates **only by the run
label**, verifies the ownership of each, removes by **ID** (containers, networks, volumes in this order) and whatever is not its own, cannot be checked (inspection failure) or merely carries a similar
name is **kept and reported**. The run ID is mandatory and must have the exact shape (a prefix, a pattern or `*` is not enough); `--dry-run` removes nothing. The other places in the harness that formerly removed by
a planned name (pre-cleanup of a container before start, `compose down`, adoption of an existing network) now verify ownership before intervening, otherwise they refuse and change nothing.

## 5. Behaviour in non-standard states

| State | Behaviour |
|---|---|
| tunnel on | cloudflared on `tunnel` → nginx trusts `CF-Connecting-IP` → the client is preserved |
| **tunnel off** (container stopped or missing) | nginx runs unchanged (healthy, RestartCount 0, 80/8443/8444 respond); `tunnel` has no member except nginx; the public name in Cloudflare has no origin, otherwise nothing changes; spoofing from `edge` is still ignored |
| replacement of the cloudflared container (a different address in the subnet) | trust is network membership, not an address: the new address is trusted again without an nginx reload |
| nginx replacement | nginx's address on `tunnel` is written nowhere; the name `edu_ai_nginx` is resolved again |
| cloudflared accidentally in another network (e.g. back in `edge`) | the header is **ignored** (safe fall): the rate limit for the tunnel becomes shared, never forged |
| cloudflared in `edge` **and** `tunnel` | lab: the name `edu_ai_nginx` resolved only to the address in `edge` and all 12 requests were ignored (an observation of one Docker version, not a guarantee); that is why cloudflared leaves `edge` |
| an extra container in `tunnel` | becomes trusted: the boundary is membership. Nothing else may be attached to `tunnel`; the authority to attach a network belongs to the Docker administrator (the same as root on the host) |
| the subnet collides (network, pool, route, address, known range, administrator range) or cannot be verified | **the preparation is refused before anything is created**; existing networks and routing are not changed |
| the `trust.conf` file is missing (e.g. an old compose) | nginx starts and trusts nobody (the tunnel rate limit is shared, never forged) |
| two installations on one host | each has its own subnet and `tunnel`; removing one does not affect the other |

## 6. Assumptions and rollout order (an implementation step, not today)

Assumptions that **cannot be verified from the repository** and that the owner must confirm:
1. Cloudflare dashboard: no public tunnel name targets a service in `edge` other than `nginx:81`. After `edge` is removed such a name would stop working. According to the repository
   the targets are only `edu_ai_nginx:81` and `bifrost:8080`; the real dashboard was not read here.
2. **A mandatory subnet preflight on the production host** (`allocate`/`check`, `--routes host`, with `--reserved` for LAN/VPN/VPC the host does not see); collisions must be zero and the value
   is stored as an installation setting. The production state is **not verified** in this proposal.
3. A decision on the end of the S2 rollback window: the script `scripts/nginx_s2_switch.sh` assumes that S2 changes only the `nginx` block; after nginx's network change in a later commit its rollback
   (`git revert` of S2, restoring the block) would have to be assessed again.
4. Compose on the host supports `configs` with `content` and interpolation (from 2.23.1); the actual mounting of the config into the container was verified only on Docker Desktop (WSL2), not on the production Linux VM.

Order (verify each step separately and it can be rolled back; the S2 script is not used for R2, the S2 preflight refuses changes other than S2):
0. Preflight and storing `EDU_AI_TUNNEL_SUBNET` (the only settings change; without it the new compose does not render).
1. `nginx.conf` with the `include` of the file from Compose and `X-Forwarded-For $remote_addr`, **the old range `172.16.0.0/12` kept**; `nginx -t`; `nginx -s reload` (a directory mount makes this possible,
   without a container replacement). This does not yet change whom nginx trusts (the file is not mounted yet). Lab: variant `transition`.
2. Compose with the networks and the config: recreate only `nginx` and `cloudflared` (explicit definition and pinned image ID as with S2, no general `up`); nginx now trusts the new and the old range,
   cloudflared is already in `tunnel`; verify the public tunnel name and preservation of the client address. Lab: variant `transition2`.
3. Remove the `172.16.0.0/12` line; `nginx -t`; reload; verify that spoofing from `edge` no longer passes. Lab: variant `candidate`.
Rollback in reverse order; after step 1 nothing depends on the network, after step 3 restore the line.

## 7. What the proposal does not address and risks

* **Direct calls to applications by a member of `edge`.** Every member of `edge` can call `edu_ai_api:8080` and others directly and send them `X-Real-IP` and `X-Forwarded-*`. This can be closed only by separating the applications
  from unauthorised senders (S5, a dedicated nginx → applications network). Therefore **no application may trust these headers** and `FORWARDED_ALLOW_IPS=*` is not used.
* Port 81 stays reachable from `edge` (as itself). Narrowing `listen` to an address in `tunnel` would require a fixed nginx address; it is not proposed.
* `X-Forwarded-Proto` is always `http` (TLS terminates before nginx); the real scheme is not passed on.
* Source address forgery at the L2 layer (raw sockets, ARP) and pgAdmin (`ProxyFix`) and Langflow were not tested; for pgAdmin the new `X-Forwarded-For` contains the same last entry as before.
* The preflight sees only what the host knows. A network the host does not know about (a remote LAN behind another gateway, a VPN later) may collide only later; that is why `check` is repeated and the administrator gives `--reserved`.
  The subnet does not change itself on an overlap with a later route, the check only refuses further creation or update.
* The lab (and hence the preflight) read routes and addresses via the namespace of the Docker Desktop daemon, not via the Windows host or the production Linux. `table all` is verified for the pinned helper image
  (BusyBox 1.37); `--routes host` with a real iproute2 on Linux, tables numbered above 255 (e.g. `wg-quick` 51820) in a real BusyBox, and a helper image that would silently ignore `table all` are not verified
  (limitation).

## 8. Recommended minimal implementation step

A single change unit from the two diffs above (compose + `nginx/conf`) and from the subnet allocation/preflight tool, rolled out according to section 6 after confirming assumptions 1–4 and the owner's explicit consent;
without changing the applications. Before that, move the checks of CIDR agreement, `tunnel` membership and the preflight into the regular tests and fix the comments in `docker-compose.yml`. The broader S5 and the UNIX socket
remain separate decisions. **The lab proposal is confirmed after the reviews R2-R1–R3 within the documented scope.
The implementation candidate (section 6 as a script, with a rehearsal of the transition and rollback) is ready for review: checkpoint;
production deployment requires a review of the candidate, closing the unverified points (Cloudflare/Access, a real cloudflared) and the owner's explicit consent.**
