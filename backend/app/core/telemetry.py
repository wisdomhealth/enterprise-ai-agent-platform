from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

HTTP_REQUESTS = Counter(
    "platform_http_requests_total",
    "HTTP requests handled by the platform.",
    ("method", "path", "status"),
)


def record_http_request(method: str, path: str, status_code: int) -> None:
    HTTP_REQUESTS.labels(method=method, path=path, status=str(status_code)).inc()


def prometheus_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
