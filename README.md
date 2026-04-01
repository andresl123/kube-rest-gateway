# kube-rest-gateway

A lightweight **FastAPI** server that acts as a secure HTTP proxy between an AI
agent and a Kubernetes cluster.

The agent lives in a sandbox (OpenShell) that blocks direct access to the K8s
API port (6443). This gateway listens on an allowed port (default **8081**),
accepts REST requests from the agent, and talks to the cluster on its behalf
using the official `kubernetes` Python client.

---

## Project layout

```
kube-rest-gateway/
├── main.py          # FastAPI app — all endpoints live here
├── config.py        # Configuration (env-var driven)
├── requirements.txt # Python dependencies
└── README.md
```

---

## Quick-start

### 1. Install dependencies

```bash
# Create & activate a virtual environment (recommended)
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure the gateway

All settings are read from **environment variables** — no secrets in source code.

| Variable | Default | Description |
|---|---|---|
| `KUBECONFIG_PATH` | `~/.kube/config` | Path to your kubeconfig file |
| `GATEWAY_API_TOKEN` | *(empty — auth disabled)* | Shared bearer token the agent must send |
| `GATEWAY_HOST` | `0.0.0.0` | Interface to bind (`0.0.0.0` allows external access) |
| `GATEWAY_PORT` | `8081` | Listening port |
| `DEFAULT_NAMESPACE` | `default` | Namespace used when none is specified |
| `DEFAULT_LOG_TAIL_LINES` | `100` | Lines returned by `/api/logs/…` by default |
| `LOG_LEVEL` | `INFO` | Uvicorn / app log level |

#### Generate a token

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

#### Set environment variables (Windows PowerShell)

```powershell
$env:KUBECONFIG_PATH  = "C:\Users\Admin\.kube\restricted-user.yaml"
$env:GATEWAY_API_TOKEN = "paste-your-generated-token-here"
```

#### Set environment variables (Linux / macOS / bash)

```bash
export KUBECONFIG_PATH="$HOME/.kube/restricted-user.yaml"
export GATEWAY_API_TOKEN="paste-your-generated-token-here"
```

### 3. Run the server

```bash
python main.py
```

You should see:

```
INFO  kube-gateway — Kubernetes client initialised from: C:\Users\Admin\.kube\restricted-user.yaml
INFO  uvicorn.error — Application startup complete.
INFO  uvicorn.error — Uvicorn running on http://YOUR_SERVER_IP:8081
```

The **interactive docs** (Swagger UI) are available at:
`http://YOUR_SERVER_IP:8081/docs`

---

## API reference

All endpoints (except `/healthz`) require the header:

```
Authorization: Bearer <GATEWAY_API_TOKEN>
```

### `GET /healthz`
Liveness probe — no auth required.

### `GET /api/pods`
List pods in a namespace.

| Query param | Default | Description |
|---|---|---|
| `namespace` | `default` | Kubernetes namespace |
| `label_selector` | — | e.g. `app=nginx` |
| `field_selector` | — | e.g. `status.phase=Running` |

### `GET /api/pods/{pod_name}`
Describe a single pod.

### `GET /api/logs/{pod_name}`
Tail pod logs.

| Query param | Default | Description |
|---|---|---|
| `namespace` | `default` | Kubernetes namespace |
| `container` | — | Container name (required for multi-container pods) |
| `tail_lines` | `100` | Lines to return (max 10 000) |
| `previous` | `false` | Return logs of the previous (crashed) instance |

### `GET /api/services`
List services in a namespace.

### `GET /api/deployments`
List deployments in a namespace.

### `GET /api/nodes`
List cluster nodes (cluster-scoped, no namespace param).

### `GET /api/namespaces`
List all namespaces (cluster-scoped).

### `GET /api/events`
List recent events — great for diagnosing pod failures.

| Query param | Default | Description |
|---|---|---|
| `namespace` | `default` | Kubernetes namespace |
| `field_selector` | — | e.g. `involvedObject.name=my-pod` |

---

## curl test examples

> Replace `YOUR_TOKEN` with your actual `GATEWAY_API_TOKEN` value.

```bash
# ── Liveness probe (no auth needed) ──────────────────────────────────────────
curl http://YOUR_SERVER_IP:8081/healthz

# ── List pods in the default namespace ───────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://YOUR_SERVER_IP:8081/api/pods

# ── List pods in a specific namespace ────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     "http://YOUR_SERVER_IP:8081/api/pods?namespace=kube-system"

# ── Filter pods by label ─────────────────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     "http://YOUR_SERVER_IP:8081/api/pods?label_selector=app%3Dnginx"

# ── Describe a single pod ─────────────────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://YOUR_SERVER_IP:8081/api/pods/my-pod-7d9f8b-xkqzp

# ── Fetch the last 50 log lines from a pod ───────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     "http://YOUR_SERVER_IP:8081/api/logs/my-pod-7d9f8b-xkqzp?tail_lines=50"

# ── Logs for a specific container in a multi-container pod ───────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     "http://YOUR_SERVER_IP:8081/api/logs/my-pod-7d9f8b-xkqzp?container=sidecar&tail_lines=200"

# ── List services ─────────────────────────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://YOUR_SERVER_IP:8081/api/services

# ── List deployments ──────────────────────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://YOUR_SERVER_IP:8081/api/deployments

# ── List nodes ────────────────────────────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://YOUR_SERVER_IP:8081/api/nodes

# ── List namespaces ───────────────────────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://YOUR_SERVER_IP:8081/api/namespaces

# ── Events for a specific pod ─────────────────────────────────────────────────
curl -H "Authorization: Bearer YOUR_TOKEN" \
     "http://YOUR_SERVER_IP:8081/api/events?field_selector=involvedObject.name%3Dmy-pod-7d9f8b-xkqzp"

# ── Verify auth rejection (expect 401) ───────────────────────────────────────
curl -H "Authorization: Bearer wrong-token" \
     http://YOUR_SERVER_IP:8081/api/pods
```

---

## Error responses

All errors are returned as structured JSON so the AI agent can parse them:

```json
{
  "detail": {
    "error": "Not Found",
    "k8s_status": 404,
    "message": "pods \"bad-pod\" not found",
    "resource": "pod/default/bad-pod"
  }
}
```

| Scenario | HTTP status |
|---|---|
| Invalid / missing token | `401 Unauthorized` |
| RBAC permission denied | `403 Forbidden` |
| Resource not found | `404 Not Found` |
| K8s API server error | `502 Bad Gateway` |

---

## Security notes

- Use a **restricted RBAC user** in your kubeconfig — give it only the
  permissions the agent actually needs (ideally `get`/`list`/`watch` on specific
  resources only).
- Set `GATEWAY_API_TOKEN` to a long random string. Rotate it if compromised.
- Bind the server to your desired interface (`0.0.0.0` for all network access).
- The `/healthz` endpoint intentionally skips auth so orchestration tools
  (Docker health-check, Kubernetes liveness probe) can use it without a token.
