"""
main.py — kube-rest-gateway
============================
A lightweight FastAPI server that acts as a secure proxy between an AI agent
and a Kubernetes cluster.  The agent cannot reach the K8s API directly
(port 6443 is blocked in its sandbox), so it talks to this gateway on an
allowed port (default 8081).

Endpoints
---------
GET  /healthz                              — liveness probe
GET  /api/pods?namespace=<ns>             — list pods
GET  /api/pods/{pod_name}?namespace=<ns>  — describe a single pod
GET  /api/services?namespace=<ns>         — list services
GET  /api/deployments?namespace=<ns>      — list deployments
GET  /api/nodes                           — list cluster nodes
GET  /api/namespaces                      — list namespaces
GET  /api/logs/{pod_name}
         ?namespace=<ns>                  — tail pod logs
         &container=<c>
         &tail_lines=<n>
GET  /api/events?namespace=<ns>           — list recent events

Authentication
--------------
Set the env var GATEWAY_API_TOKEN to a shared secret.  Every request must
carry:  Authorization: Bearer <token>
If GATEWAY_API_TOKEN is empty the check is skipped (dev-only convenience).
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

import config as cfg

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kube-gateway")

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Kube REST Gateway",
    description=(
        "A secure REST proxy that lets an AI agent interact with a "
        "Kubernetes cluster through a firewall-friendly HTTP interface."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Kubernetes client initialisation ─────────────────────────────────────────


def _init_k8s_client() -> None:
    """Load the kubeconfig file once at startup."""
    try:
        k8s_config.load_kube_config(config_file=cfg.KUBECONFIG_PATH)
        logger.info("Kubernetes client initialised from: %s", cfg.KUBECONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load kubeconfig '%s': %s", cfg.KUBECONFIG_PATH, exc)
        raise RuntimeError(
            f"Cannot load kubeconfig from '{cfg.KUBECONFIG_PATH}': {exc}"
        ) from exc


@app.on_event("startup")
async def startup_event() -> None:
    _init_k8s_client()


# ── Authentication ────────────────────────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


def verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """Validate the bearer token when GATEWAY_API_TOKEN is configured."""
    if not cfg.API_TOKEN:
        # Token auth is disabled — useful for local dev / trusted networks.
        logger.debug("API token auth disabled; skipping credential check.")
        return

    if credentials is None or credentials.credentials != cfg.API_TOKEN:
        logger.warning("Rejected request — invalid or missing bearer token.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Unauthorized", "message": "Invalid or missing bearer token."},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _k8s_error_response(exc: ApiException, resource: str = "resource") -> HTTPException:
    """Convert a kubernetes ApiException into a FastAPI HTTPException."""
    logger.error("Kubernetes API error [%s] for %s: %s", exc.status, resource, exc.reason)

    # Map common K8s status codes to meaningful HTTP responses.
    code_map = {
        401: status.HTTP_401_UNAUTHORIZED,
        403: status.HTTP_403_FORBIDDEN,
        404: status.HTTP_404_NOT_FOUND,
        409: status.HTTP_409_CONFLICT,
        422: status.HTTP_422_UNPROCESSABLE_ENTITY,
        429: status.HTTP_429_TOO_MANY_REQUESTS,
        500: status.HTTP_500_INTERNAL_SERVER_ERROR,
        503: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    http_status = code_map.get(exc.status, status.HTTP_502_BAD_GATEWAY)

    return HTTPException(
        status_code=http_status,
        detail={
            "error": exc.reason or "KubernetesApiError",
            "k8s_status": exc.status,
            "message": exc.body.decode() if isinstance(exc.body, bytes) else str(exc.body),
            "resource": resource,
        },
    )


def _pod_to_dict(pod: Any) -> Dict[str, Any]:
    """Serialize a V1Pod object to a clean dict."""
    containers = pod.spec.containers or []
    container_statuses = pod.status.container_statuses or []

    status_map: Dict[str, Any] = {}
    for cs in container_statuses:
        status_map[cs.name] = {
            "ready": cs.ready,
            "restart_count": cs.restart_count,
            "image": cs.image,
        }

    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "phase": pod.status.phase,
        "pod_ip": pod.status.pod_ip,
        "node_name": pod.spec.node_name,
        "creation_timestamp": (
            pod.metadata.creation_timestamp.isoformat()
            if pod.metadata.creation_timestamp
            else None
        ),
        "labels": pod.metadata.labels or {},
        "containers": [
            {
                "name": c.name,
                "image": c.image,
                "ports": [
                    {"container_port": p.container_port, "protocol": p.protocol}
                    for p in (c.ports or [])
                ],
                **status_map.get(c.name, {}),
            }
            for c in containers
        ],
        "conditions": [
            {"type": cond.type, "status": cond.status}
            for cond in (pod.status.conditions or [])
        ],
    }


def _svc_to_dict(svc: Any) -> Dict[str, Any]:
    """Serialize a V1Service object to a clean dict."""
    spec = svc.spec
    return {
        "name": svc.metadata.name,
        "namespace": svc.metadata.namespace,
        "type": spec.type,
        "cluster_ip": spec.cluster_ip,
        "external_ips": spec.external_i_ps or [],
        "load_balancer_ip": spec.load_balancer_ip,
        "ports": [
            {
                "name": p.name,
                "protocol": p.protocol,
                "port": p.port,
                "target_port": str(p.target_port),
                "node_port": p.node_port,
            }
            for p in (spec.ports or [])
        ],
        "selector": spec.selector or {},
        "creation_timestamp": (
            svc.metadata.creation_timestamp.isoformat()
            if svc.metadata.creation_timestamp
            else None
        ),
    }


def _deployment_to_dict(dep: Any) -> Dict[str, Any]:
    """Serialize a V1Deployment object to a clean dict."""
    return {
        "name": dep.metadata.name,
        "namespace": dep.metadata.namespace,
        "creation_timestamp": (
            dep.metadata.creation_timestamp.isoformat()
            if dep.metadata.creation_timestamp
            else None
        ),
        "labels": dep.metadata.labels or {},
        "replicas": {
            "desired": dep.spec.replicas,
            "ready": dep.status.ready_replicas,
            "available": dep.status.available_replicas,
            "updated": dep.status.updated_replicas,
        },
        "strategy": dep.spec.strategy.type if dep.spec.strategy else None,
        "conditions": [
            {
                "type": c.type,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
            }
            for c in (dep.status.conditions or [])
        ],
    }


def _node_to_dict(node: Any) -> Dict[str, Any]:
    """Serialize a V1Node object to a clean dict."""
    info = node.status.node_info
    conditions = [
        {"type": c.type, "status": c.status} for c in (node.status.conditions or [])
    ]
    addresses = {a.type: a.address for a in (node.status.addresses or [])}
    capacity = {
        k: str(v) for k, v in (node.status.capacity or {}).items()
    }
    return {
        "name": node.metadata.name,
        "creation_timestamp": (
            node.metadata.creation_timestamp.isoformat()
            if node.metadata.creation_timestamp
            else None
        ),
        "labels": node.metadata.labels or {},
        "addresses": addresses,
        "capacity": capacity,
        "os_image": info.os_image if info else None,
        "kernel_version": info.kernel_version if info else None,
        "kubelet_version": info.kubelet_version if info else None,
        "container_runtime": info.container_runtime_version if info else None,
        "conditions": conditions,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/healthz", tags=["Health"])
async def healthz() -> Dict[str, str]:
    """Liveness probe — always returns 200 if the server is running."""
    return {"status": "ok", "service": "kube-rest-gateway"}


@app.get("/", tags=["Health"])
async def root() -> Dict[str, str]:
    """Root redirect to help."""
    return {
        "message": "Welcome to Kube-REST-Gateway. Use /api/help for instructions.",
        "docs": "/docs",
        "help": "/api/help",
    }


@app.get("/api/help", tags=["Health"], dependencies=[Depends(verify_token)])
async def api_help() -> Dict[str, Any]:
    """
    Returns a structured guide for the AI agent to understand
    how to interact with this cluster.
    """
    return {
        "gateway_info": {
            "version": "1.0.0",
            "purpose": "Secure Kubernetes API proxy for AI Agents",
            "auth": "Bearer Token required in 'Authorization' header",
        },
        "capabilities": [
            {
                "endpoint": "/api/pods",
                "method": "GET",
                "description": "List all pods. Use ?namespace= to filter.",
                "examples": ["/api/pods?namespace=default", "/api/pods?label_selector=app=nginx"],
            },
            {
                "endpoint": "/api/pods/{name}",
                "method": "GET",
                "description": "Get detailed JSON for a specific pod.",
            },
            {
                "endpoint": "/api/logs/{name}",
                "method": "GET",
                "description": "Fetch recent logs. Use ?tail_lines= to limit output.",
                "params": ["namespace", "container", "tail_lines", "previous"],
            },
            {
                "endpoint": "/api/services",
                "method": "GET",
                "description": "List all services in a namespace.",
            },
            {
                "endpoint": "/api/deployments",
                "method": "GET",
                "description": "List deployments and checkout replica health.",
            },
            {
                "endpoint": "/api/events",
                "method": "GET",
                "description": "Get cluster events to debug crashing pods (ImagePullBackOff, etc).",
                "params": ["namespace", "field_selector"],
            },
            {
                "endpoint": "/api/namespaces",
                "method": "GET",
                "description": "List all available namespaces in the cluster.",
            },
            {
                "endpoint": "/api/nodes",
                "method": "GET",
                "description": "List physical/virtual nodes in the cluster.",
            },
        ],
        "instructions": (
            "1. Always check /api/namespaces first if you aren't sure where to look. "
            "2. If a pod is 'Pending' or 'Error', check /api/events for the root cause. "
            "3. Keep log requests small (e.g., tail_lines=50) to save processing time."
        ),
    }


# ── Pods ──────────────────────────────────────────────────────────────────────


@app.get("/api/pods", tags=["Pods"], dependencies=[Depends(verify_token)])
async def list_pods(
    namespace: str = Query(default=cfg.DEFAULT_NAMESPACE, description="Kubernetes namespace"),
    label_selector: Optional[str] = Query(default=None, description="Label selector, e.g. app=nginx"),
    field_selector: Optional[str] = Query(default=None, description="Field selector, e.g. status.phase=Running"),
) -> Dict[str, Any]:
    """
    List all pods in a namespace.

    Returns structured JSON with the most useful pod attributes.
    """
    v1 = client.CoreV1Api()
    try:
        kwargs: Dict[str, Any] = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if field_selector:
            kwargs["field_selector"] = field_selector

        resp = v1.list_namespaced_pod(namespace=namespace, **kwargs)
    except ApiException as exc:
        raise _k8s_error_response(exc, resource=f"pods/{namespace}") from exc

    pods: List[Dict[str, Any]] = [_pod_to_dict(p) for p in resp.items]
    return {
        "namespace": namespace,
        "count": len(pods),
        "pods": pods,
    }


@app.get("/api/pods/{pod_name}", tags=["Pods"], dependencies=[Depends(verify_token)])
async def get_pod(
    pod_name: str,
    namespace: str = Query(default=cfg.DEFAULT_NAMESPACE, description="Kubernetes namespace"),
) -> Dict[str, Any]:
    """Describe a single pod by name."""
    v1 = client.CoreV1Api()
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as exc:
        raise _k8s_error_response(exc, resource=f"pod/{namespace}/{pod_name}") from exc

    return _pod_to_dict(pod)


# ── Logs ──────────────────────────────────────────────────────────────────────


@app.get("/api/logs/{pod_name}", tags=["Logs"], dependencies=[Depends(verify_token)])
async def get_pod_logs(
    pod_name: str,
    namespace: str = Query(default=cfg.DEFAULT_NAMESPACE, description="Kubernetes namespace"),
    container: Optional[str] = Query(default=None, description="Container name (required for multi-container pods)"),
    tail_lines: int = Query(default=cfg.DEFAULT_LOG_TAIL_LINES, ge=1, le=10000, description="Number of recent log lines to return"),
    previous: bool = Query(default=False, description="Return logs of the previous (crashed) container instance"),
) -> Dict[str, Any]:
    """
    Fetch recent logs for a pod.

    The `tail_lines` parameter limits output size so the agent doesn't get
    flooded (default: 100 lines, max: 10 000).
    """
    v1 = client.CoreV1Api()
    try:
        kwargs: Dict[str, Any] = dict(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            previous=previous,
        )
        if container:
            kwargs["container"] = container

        raw_logs: str = v1.read_namespaced_pod_log(**kwargs)
    except ApiException as exc:
        raise _k8s_error_response(exc, resource=f"logs/{namespace}/{pod_name}") from exc

    lines = raw_logs.splitlines()
    return {
        "pod_name": pod_name,
        "namespace": namespace,
        "container": container,
        "tail_lines_requested": tail_lines,
        "lines_returned": len(lines),
        "logs": lines,
    }


# ── Services ─────────────────────────────────────────────────────────────────


@app.get("/api/services", tags=["Services"], dependencies=[Depends(verify_token)])
async def list_services(
    namespace: str = Query(default=cfg.DEFAULT_NAMESPACE, description="Kubernetes namespace"),
    label_selector: Optional[str] = Query(default=None, description="Label selector"),
) -> Dict[str, Any]:
    """List all services in a namespace."""
    v1 = client.CoreV1Api()
    try:
        kwargs: Dict[str, Any] = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        resp = v1.list_namespaced_service(namespace=namespace, **kwargs)
    except ApiException as exc:
        raise _k8s_error_response(exc, resource=f"services/{namespace}") from exc

    services = [_svc_to_dict(s) for s in resp.items]
    return {
        "namespace": namespace,
        "count": len(services),
        "services": services,
    }


# ── Deployments ───────────────────────────────────────────────────────────────


@app.get("/api/deployments", tags=["Deployments"], dependencies=[Depends(verify_token)])
async def list_deployments(
    namespace: str = Query(default=cfg.DEFAULT_NAMESPACE, description="Kubernetes namespace"),
    label_selector: Optional[str] = Query(default=None, description="Label selector"),
) -> Dict[str, Any]:
    """List all deployments in a namespace."""
    apps_v1 = client.AppsV1Api()
    try:
        kwargs: Dict[str, Any] = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        resp = apps_v1.list_namespaced_deployment(namespace=namespace, **kwargs)
    except ApiException as exc:
        raise _k8s_error_response(exc, resource=f"deployments/{namespace}") from exc

    deployments = [_deployment_to_dict(d) for d in resp.items]
    return {
        "namespace": namespace,
        "count": len(deployments),
        "deployments": deployments,
    }


# ── Nodes ─────────────────────────────────────────────────────────────────────


@app.get("/api/nodes", tags=["Cluster"], dependencies=[Depends(verify_token)])
async def list_nodes() -> Dict[str, Any]:
    """List all nodes in the cluster."""
    v1 = client.CoreV1Api()
    try:
        resp = v1.list_node()
    except ApiException as exc:
        raise _k8s_error_response(exc, resource="nodes") from exc

    nodes = [_node_to_dict(n) for n in resp.items]
    return {"count": len(nodes), "nodes": nodes}


# ── Namespaces ────────────────────────────────────────────────────────────────


@app.get("/api/namespaces", tags=["Cluster"], dependencies=[Depends(verify_token)])
async def list_namespaces() -> Dict[str, Any]:
    """List all namespaces in the cluster."""
    v1 = client.CoreV1Api()
    try:
        resp = v1.list_namespace()
    except ApiException as exc:
        raise _k8s_error_response(exc, resource="namespaces") from exc

    namespaces = [
        {
            "name": ns.metadata.name,
            "phase": ns.status.phase,
            "creation_timestamp": (
                ns.metadata.creation_timestamp.isoformat()
                if ns.metadata.creation_timestamp
                else None
            ),
            "labels": ns.metadata.labels or {},
        }
        for ns in resp.items
    ]
    return {"count": len(namespaces), "namespaces": namespaces}


# ── Events ────────────────────────────────────────────────────────────────────


@app.get("/api/events", tags=["Cluster"], dependencies=[Depends(verify_token)])
async def list_events(
    namespace: str = Query(default=cfg.DEFAULT_NAMESPACE, description="Kubernetes namespace"),
    field_selector: Optional[str] = Query(default=None, description="Field selector, e.g. involvedObject.name=my-pod"),
) -> Dict[str, Any]:
    """
    List recent events in a namespace.

    Useful for diagnosing pod failures, ImagePullBackOff, etc.
    """
    v1 = client.CoreV1Api()
    try:
        kwargs: Dict[str, Any] = {}
        if field_selector:
            kwargs["field_selector"] = field_selector
        resp = v1.list_namespaced_event(namespace=namespace, **kwargs)
    except ApiException as exc:
        raise _k8s_error_response(exc, resource=f"events/{namespace}") from exc

    events = [
        {
            "name": ev.metadata.name,
            "namespace": ev.metadata.namespace,
            "type": ev.type,
            "reason": ev.reason,
            "message": ev.message,
            "count": ev.count,
            "first_timestamp": ev.first_timestamp.isoformat() if ev.first_timestamp else None,
            "last_timestamp": ev.last_timestamp.isoformat() if ev.last_timestamp else None,
            "involved_object": {
                "kind": ev.involved_object.kind,
                "name": ev.involved_object.name,
                "namespace": ev.involved_object.namespace,
            },
            "source": {
                "component": ev.source.component if ev.source else None,
                "host": ev.source.host if ev.source else None,
            },
        }
        for ev in resp.items
    ]

    # Sort most recent first.
    events.sort(key=lambda e: e["last_timestamp"] or "", reverse=True)

    return {
        "namespace": namespace,
        "count": len(events),
        "events": events,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=cfg.HOST,
        port=cfg.PORT,
        log_level=cfg.LOG_LEVEL.lower(),
        reload=False,
    )
