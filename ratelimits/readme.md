# Ratelimit Usecase

Demonstriert Istio Local Rate Limiting: ein `nginx`-Deployment (2 Replicas) in einem
eigenen Namespace, abgesichert durch einen `EnvoyFilter`, der pro Sidecar nur eine
begrenzte Anzahl Requests pro Zeitfenster zulässt. Ein Lasttest-Pod zeigt anschließend
die resultierenden HTTP-Returncodes (200 vs. 429).

## Verzeichnisstruktur

```
ratelimits/
  install.sh                 # installiert alle Manifeste
  run.sh                      # startet den Lasttest
  manifests/
    00-namespace.yaml                    # Namespace (Default: ratelimit-demo, istio-injection: enabled)
    10-nginx-deployment.yaml             # Deployment nginx, replicas: 2
    11-nginx-service.yaml                # ClusterIP Service nginx
    20-envoyfilter-local-ratelimit.yaml  # Envoy Local Rate Limit auf den nginx-Sidecars
  test/
    loadtest-pod.yaml          # Pod-Template für den Lasttest-Container (curl-Schleife)
```

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster

## Installation

```bash
./install.sh                     # Standard-Namespace: ratelimit-demo
./install.sh mein-namespace      # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml` übersprungen
(kein erneutes Anlegen/Überschreiben) — nur Deployment, Service und EnvoyFilter
werden in diesen bestehenden Namespace appliziert.

Führt intern aus (Platzhalter `${NAMESPACE}` in den Manifesten werden per `sed`
durch den gewählten Namespace ersetzt):

```bash
kubectl get namespace <namespace>                       # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml | kubectl apply -f -   # nur falls Namespace neu
sed "s|\${NAMESPACE}|<namespace>|g" manifests/10-nginx-deployment.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/11-nginx-service.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/20-envoyfilter-local-ratelimit.yaml | kubectl apply -f -
kubectl -n <namespace> rollout status deployment/nginx --timeout=120s
kubectl -n <namespace> get deploy,svc,envoyfilter
```

**Hinweis:** Wird ein bereits existierender Namespace verwendet, muss dieser für
das EnvoyFilter-Rate-Limiting selbst das Label `istio-injection: enabled` tragen
(bzw. Sidecar-Injection anderweitig aktiviert haben), sonst greift kein Sidecar
und damit auch kein Rate Limit.

## Lasttest ausführen

```bash
./run.sh                         # Standard: Namespace ratelimit-demo, 30 Requests
./run.sh mein-namespace          # eigener Namespace, 30 Requests
./run.sh mein-namespace 50       # eigener Namespace, eigene Anzahl Requests
```

Führt intern aus (Platzhalter aus `test/loadtest-pod.yaml` werden per `sed` ersetzt):

```bash
sed -e "s|\${POD_NAME}|<generierter-name>|g" \
    -e "s|\${NAMESPACE}|<namespace>|g" \
    -e "s|\${TARGET}|http://nginx.<namespace>.svc.cluster.local|g" \
    -e "s|\${REQUESTS}|30|g" \
    test/loadtest-pod.yaml | kubectl apply -f -

kubectl -n <namespace> wait --for=jsonpath='{.status.phase}'=Succeeded pod/<generierter-name> --timeout=90s
kubectl -n <namespace> logs <generierter-name>
kubectl -n <namespace> delete pod <generierter-name>
```

Der Lasttest-Pod läuft **ohne** Istio-Sidecar (`sidecar.istio.io/inject: "false"`),
da das Rate Limiting server­seitig am Sidecar von `nginx` durchgesetzt wird — der
Client muss dafür nicht Teil des Mesh sein.

Der Lasttest-Pod gibt pro Request alle Response-Header aus (`curl -s -D - -o /dev/null`),
sodass z.B. auch der von Envoy gesetzte Header `x-local-rate-limit: true` sichtbar ist.

Beispielhafte Ausgabe:

```
=== request 1 ===
HTTP/1.1 200 OK
server: envoy
date: ...
content-type: text/html
...
=== request 3 ===
HTTP/1.1 429 Too Many Requests
server: envoy
x-local-rate-limit: true
...
==> Summary of HTTP status codes:
     27 200
      3 429
```

Aktuell ist im EnvoyFilter (`manifests/20-envoyfilter-local-ratelimit.yaml`) ein
Token-Bucket von 5 Tokens/30s hinterlegt.

## Aufräumen

```bash
kubectl delete namespace <namespace>   # z.B. ratelimit-demo
```
