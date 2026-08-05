# Ratelimit Full (Global Rate Limiting) Usecase

Demonstriert Istio **Global Rate Limiting** im Gegensatz zum
[`ratelimits`](../ratelimits/readme.md)-Usecase (Local Rate Limiting):

- Beim **Local** Rate Limiting hat **jeder** Envoy-Sidecar seinen eigenen,
  unabhängigen Token-Bucket — das effektive Gesamtlimit skaliert also mit der
  Anzahl der Replicas.
- Beim **Global** Rate Limiting fragen **alle** Sidecars vor jedem Request einen
  zentralen Rate-Limit-Service (gRPC), der den Zählerstand in Redis führt. Das
  Limit gilt damit über alle nginx-Replicas **gemeinsam**.

## Architektur

```
Client -> nginx-Sidecar --(gRPC ShouldRateLimit)--> ratelimit-Service -> Redis
                        --(falls erlaubt)--> nginx-Container
```

## Verzeichnisstruktur

```
ratelimitService/
  install.sh                                    # installiert alle Manifeste
  uninstall.sh                                   # entfernt die Ressourcen dieses Usecases wieder
  run.sh                                         # startet den Lasttest
  manifests/
    00-namespace.yaml                            # Namespace (Default: ratelimit-service-demo, istio-injection: enabled)
    01-redis.yaml                                 # Redis Deployment/Service, Backend für den Ratelimit-Service
    02-ratelimit-config.yaml                      # ConfigMap: Domain + Descriptor + Limit (5 Requests/Minute)
    03-ratelimit-service.yaml                     # envoyproxy/ratelimit Deployment/Service (gRPC Port 8081)
    05-nginx-configmap.yaml                       # nginx.conf, Server lauscht auf Port 8080
    10-nginx-deployment.yaml                      # Deployment nginx, replicas: 2, containerPort 8080
    11-nginx-service.yaml                         # ClusterIP Service nginx (port 80 -> targetPort 8080)
    20-envoyfilter-ratelimit-filter.yaml           # fügt den envoy.filters.http.ratelimit HTTP-Filter ein
    21-envoyfilter-ratelimit-route.yaml            # aktiviert das Limit auf der Inbound-Route via rate_limits-Action
  test/
    loadtest-pod.yaml                              # Pod-Template für den Lasttest-Container (curl-Schleife)
```

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster

## Installation

```bash
./install.sh                          # Standard-Namespace: ratelimit-service-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml` übersprungen
(kein erneutes Anlegen/Überschreiben) — nur Redis, Ratelimit-Service, nginx und
die beiden EnvoyFilter werden in diesen bestehenden Namespace appliziert.

Führt intern aus (Platzhalter `${NAMESPACE}` in den Manifesten werden per `sed`
durch den gewählten Namespace ersetzt):

```bash
kubectl get namespace <namespace>                       # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml | kubectl apply -f -   # nur falls Namespace neu
sed "s|\${NAMESPACE}|<namespace>|g" manifests/01-redis.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/02-ratelimit-config.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/03-ratelimit-service.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/05-nginx-configmap.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/10-nginx-deployment.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/11-nginx-service.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/20-envoyfilter-ratelimit-filter.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/21-envoyfilter-ratelimit-route.yaml | kubectl apply -f -
kubectl -n <namespace> rollout status deployment/redis --timeout=120s
kubectl -n <namespace> rollout status deployment/ratelimit --timeout=120s
kubectl -n <namespace> rollout status deployment/nginx --timeout=120s
kubectl -n <namespace> get deploy,svc,envoyfilter
```

**Hinweis:** Wird ein bereits existierender Namespace verwendet, muss dieser das
Label `istio-injection: enabled` tragen (bzw. Sidecar-Injection anderweitig
aktiviert haben), sonst greift kein Sidecar und damit auch kein Rate Limit.

## Lasttest ausführen

```bash
./run.sh                         # Standard: Namespace ratelimit-service-demo, 30 Requests
./run.sh mein-namespace          # eigener Namespace, 30 Requests
./run.sh mein-namespace 50       # eigener Namespace, eigene Anzahl Requests
```

Da das Limit (5 Requests/Minute) **global** über beide nginx-Replicas gilt,
sollte man ab dem 6. Request innerhalb einer Minute `429`-Antworten sehen —
unabhängig davon, welches der beiden Replicas den jeweiligen Request bedient hat.

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

Der Lasttest-Pod läuft **ohne** Istio-Sidecar (`sidecar.istio.io/inject: "false"`)
und gibt pro Request alle Response-Header aus (`curl -s -D - -o /dev/null`).

## Troubleshooting

Falls trotz vieler Requests immer nur `200` zurückkommt (Limit greift nicht),
liegt das meist daran, dass die von Istio intern generierten Namen für
Routenkonfiguration/Virtual-Host/Cluster in `manifests/20-*.yaml` bzw.
`manifests/21-*.yaml` nicht zur tatsächlichen Konfiguration des Clusters passen
(diese Namen sind kein stabiles öffentliches API und können sich je nach
Istio-Version leicht unterscheiden). Prüfen mit:

```bash
# tatsächlichen Cluster-Namen für den ratelimit-Service prüfen
istioctl proxy-config cluster <nginx-pod> -n <namespace> | grep ratelimit

# tatsächlichen vhost-/route-Namen der Inbound-Route prüfen
istioctl proxy-config route <nginx-pod> -n <namespace> -o json
```

und die Werte `cluster_name` (in `20-envoyfilter-ratelimit-filter.yaml`) bzw.
`vhost.name`/`route.name` (in `21-envoyfilter-ratelimit-route.yaml`) entsprechend
anpassen.

**Wichtig:** `vhost.name` folgt dem Muster `inbound|http|<servicePort>` — hier
zählt der **Service-Port** aus `11-nginx-service.yaml` (`80`), **nicht** der
`containerPort` (`8080`) aus `10-nginx-deployment.yaml`!

Logs des Ratelimit-Service (zeigt eingehende `ShouldRateLimit`-Aufrufe):

```bash
kubectl -n <namespace> logs deployment/ratelimit
```

Falls der `ratelimit`-Pod dauerhaft `1/2 Ready` bleibt bzw. im
`CrashLoopBackOff` hängt und der `ratelimit`-Container mit Exit-Code `0` sofort
wieder beendet wird: Das Image `envoyproxy/ratelimit:6f5de117` hat kein
`ENTRYPOINT` und als `CMD` nur `/bin/sh` — ohne den `command: ["/bin/ratelimit"]`
in `03-ratelimit-service.yaml` startet also nur eine Shell ohne TTY, die sofort
terminiert (kein Server läuft, keine Logs, keine `ShouldRateLimit`-Aufrufe).

Falls stattdessen bei jedem Request `500` statt `200`/`429` zurückkommt und im
Ratelimit-Service ebenfalls keine `ShouldRateLimit`-Logs auftauchen: Der Filter
matcht zwar (`failure_mode_deny: true` schlägt zu), aber der gRPC-Call selbst
scheitert. Debuggen mit Envoy-Debug-Logging auf dem nginx-Sidecar
(`istioctl proxy-config log <nginx-pod> -n <namespace> --level debug`) und
Prüfung auf `http2.invalid.header.field` / `grpc-status 14` im Log des
`ratelimit`-Sidecars. Ursache war hier: Ohne explizites `authority`-Feld im
`envoy_grpc`-Block von `20-envoyfilter-ratelimit-filter.yaml` verwendet Envoy
den `cluster_name` selbst (z.B. `outbound|8081||ratelimit....`) als
`:authority`-Header — die enthaltenen `|`-Zeichen sind als HTTP/2-Header
ungültig, der Ziel-Sidecar resettet den Stream.

## Kombinierter Einsatz mit `ratelimits/`

Dieser Usecase lässt sich gefahrlos zusammen mit
[`ratelimits/`](../ratelimits/readme.md) **im selben Namespace** installieren:
`nginx`-ConfigMap, -Deployment und -Service sind in beiden Verzeichnissen
byte-identisch (das zweite `apply` ist also ein No-Op), und alle übrigen
Ressourcennamen (EnvoyFilter, Redis, Ratelimit-Service) sind eindeutig und
kollidieren nicht.

```bash
./ratelimits/install.sh demo
./ratelimitService/install.sh demo
```

Ergebnis: Auf den `nginx`-Pods greifen dann **beide** Mechanismen gleichzeitig —
der lokale Token-Bucket pro Sidecar **und** das globale, Redis-gestützte Limit.
Ein Request muss also beide Limits bestehen.

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: ratelimit-service-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt nur die von diesem Usecase angelegten Ressourcen (EnvoyFilter, nginx,
Ratelimit-Service, Redis) — der Namespace selbst bleibt bestehen, da er z.B.
gemeinsam mit `ratelimits/` genutzt werden könnte. Soll auch der Namespace
komplett verschwinden:

```bash
kubectl delete namespace <namespace>   # z.B. ratelimit-service-demo
```
