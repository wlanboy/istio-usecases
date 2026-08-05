# Fault Injection Usecase

Demonstriert Istio **HTTP Fault Injection**: ein `VirtualService` injiziert gezielt
Fehler (Delay bzw. HTTP 500 Abort) in Requests an ein `nginx`-Deployment (2 Replicas)
— gesteuert über den Request-Header `x-fault`, sodass sich normales, verzögertes und
fehlschlagendes Verhalten gegen denselben Service gezielt provozieren lässt. Nützlich,
um Timeout-/Retry-/Circuit-Breaking-Konfiguration von Clients zu testen, ohne den
eigentlichen Service dafür verändern zu müssen.

## Architektur

```
Test-Client-Pod --curl -H "x-fault: <wert>"--> VirtualService "nginx"
                                                  |-- x-fault: delay --> fault.delay (5s fixedDelay) --> nginx-Pod
                                                  |-- x-fault: abort --> fault.abort (HTTP 500, nginx wird NICHT erreicht)
                                                  |-- (Fallback, kein/anderer Header) --> nginx-Pod ohne Fault
```

- Der `VirtualService` matcht auf den Header `x-fault` und injiziert je nach Wert
  (`delay` / `abort`) einen Fault, bevor der Request `nginx` erreicht. Alle anderen
  Requests (Fallback-Regel) laufen unverändert durch.
- Bei `abort` antwortet **Envoy selbst** mit HTTP 500 — der Request landet gar nicht
  erst bei `nginx`. Bei `delay` wird der Request nach der Wartezeit ganz normal an
  `nginx` weitergereicht.
- **Wichtig:** Wie bei [`namespaceRouting/`](../namespaceRouting/readme.md) wird die
  `VirtualService`-Regel für einen Host am **ausgehenden** Envoy-Sidecar des
  *aufrufenden* Pods ausgewertet. Der Client muss daher selbst Teil des Meshes sein
  (Sidecar-Injection aktiv), sonst greift kein Fault und der Request geht unverändert
  durch.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster

## Installation

```bash
./install.sh                          # Standard-Namespace: fault-injection-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml` übersprungen
(kein erneutes Anlegen/Überschreiben) — nur ConfigMap, Deployment, Service und
VirtualService werden in diesen bestehenden Namespace appliziert.

Führt intern aus (Platzhalter `${NAMESPACE}` in den Manifesten werden per `sed`
durch den gewählten Namespace ersetzt):

```bash
kubectl get namespace <namespace>                       # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml | kubectl apply -f -   # nur falls Namespace neu
sed "s|\${NAMESPACE}|<namespace>|g" manifests/05-nginx-configmap.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/10-nginx-deployment.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/11-nginx-service.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/20-virtualservice-fault-injection.yaml | kubectl apply -f -
kubectl -n <namespace> rollout status deployment/nginx --timeout=120s
kubectl -n <namespace> get deploy,svc,virtualservice
```

**Hinweis:** Wird ein bereits existierender Namespace verwendet, muss dieser das
Label `istio-injection: enabled` tragen (bzw. Sidecar-Injection anderweitig aktiviert
haben) — sowohl `nginx` als auch der Test-Client müssen Teil des Meshes sein, damit
die Fault-Injection-Regel greift.

## Test ausführen

```bash
./run.sh                         # Standard-Namespace: fault-injection-demo
./run.sh mein-namespace          # eigenen Namespace verwenden
```

Startet nacheinander drei Test-Client-Pods, die je einen Request mit
unterschiedlichem `x-fault`-Header gegen `http://nginx.<namespace>.svc.cluster.local`
absetzen: `none` (kein Fault), `delay` (5s Fixed-Delay) und `abort` (sofortiger
HTTP 500).

Führt intern aus (Platzhalter aus `test/client-pod.yaml` werden per `sed` ersetzt,
je einmal pro `x-fault`-Wert):

```bash
sed -e "s|\${POD_NAME}|<generierter-name>|g" \
    -e "s|\${NAMESPACE}|<namespace>|g" \
    -e "s|\${TARGET}|http://nginx.<namespace>.svc.cluster.local|g" \
    -e "s|\${FAULT_HEADER}|<wert>|g" \
    test/client-pod.yaml | kubectl apply -f -

kubectl -n <namespace> wait --for=jsonpath='{.status.phase}'=Succeeded pod/<generierter-name> --timeout=90s
kubectl -n <namespace> logs <generierter-name> -c client
kubectl -n <namespace> delete pod <generierter-name>
# ... analog für die anderen beiden x-fault-Werte
```

Erwartete Ausgabe:

```
=== Request mit Header x-fault: none an http://nginx.fault-injection-demo.svc.cluster.local ===
HTTP-Status: 200, Dauer: 0.012s

=== Request mit Header x-fault: delay an http://nginx.fault-injection-demo.svc.cluster.local ===
HTTP-Status: 200, Dauer: 5.014s

=== Request mit Header x-fault: abort an http://nginx.fault-injection-demo.svc.cluster.local ===
HTTP-Status: 500, Dauer: 0.008s
```

Der Test-Client-Pod läuft **mit** Istio-Sidecar (im Gegensatz zum Lasttest-Pod in
[`ratelimits/`](../ratelimits/readme.md)), da die Fault-Injection-Regel am
Client-seitigen Envoy ausgewertet wird. Als natives Sidecar (Istio 1.27+, K8s 1.29+,
Standard seit Istio 1.27) beendet Kubelet den `istio-proxy` automatisch, sobald der
`client`-Container terminiert — der Pod erreicht die Phase `Succeeded` ohne weiteres
Zutun.

## Troubleshooting

Kommt bei **allen** drei Requests dieselbe (fehlerfreie) Antwort zurück, unabhängig
vom `x-fault`-Header: meist eine der folgenden Ursachen.

- **Sidecar fehlt beim Client.** Prüfen mit:
  ```bash
  kubectl -n <namespace> get pod <pod-name> -o jsonpath='{.spec.containers[*].name}'
  ```
  Es müssen zwei Container erscheinen (`client` und `istio-proxy`).

- **`VirtualService` falscher Namespace.** Er muss im selben Namespace wie der
  `nginx`-Service liegen (hier `<namespace>`), damit er ohne `exportTo`/Cross-Namespace-
  Konfiguration automatisch für Aufrufer aus anderen Namespaces sichtbar ist.

- **Zu kurz nach der Installation getestet.** Istiod braucht nach dem Anwenden des
  `VirtualService` kurz Zeit, bis die Regel per xDS beim Sidecar des Clients ankommt.
  Kurz warten und `./run.sh` erneut ausführen.

Aktueller Zustand der Regel:

```bash
kubectl -n <namespace> get virtualservice nginx -o yaml
istioctl proxy-config route <client-pod> -n <namespace> -o json
```

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: fault-injection-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt nur die von diesem Usecase angelegten Ressourcen (VirtualService,
Deployment, Service, ConfigMap) — der Namespace selbst bleibt bestehen. Zum
vollständigen Entfernen:

```bash
kubectl delete namespace <namespace>   # z.B. fault-injection-demo
```
