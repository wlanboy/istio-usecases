# Namespace Routing Usecase

Demonstriert Istio **Traffic Routing basierend auf dem Source Namespace**: zwei
Clients aus unterschiedlichen Kubernetes-Namespaces (`extern` / `intern`) rufen
denselben Service (`backend`) unter demselben Hostnamen auf, werden aber vom
Mesh transparent auf **unterschiedliche Pods** verteilt — je nachdem, aus
welchem Namespace der Request kommt. Ein typischer Anwendungsfall: derselbe
logische Service soll externen und internen Aufrufern unterschiedliche
Antworten/Implementierungen liefern (z.B. reduzierter Funktionsumfang für
externe Aufrufer), ohne dass die Aufrufer das an der URL erkennen müssen.

## Architektur

```
Namespace <ns>-extern:                    Namespace <ns>:
  client-Pod --curl--> backend.<ns> --> VirtualService "backend"
                                           |-- sourceNamespace: <ns>-extern --> Subset "extern" --> backend-extern-Pod
                                           |-- sourceNamespace: <ns>-intern --> Subset "intern" --> backend-intern-Pod
Namespace <ns>-intern:                    |-- (Fallback)                   --> Subset "extern"
  client-Pod --curl--> backend.<ns> -----^
```

- Ein `Service` (`backend`) und ein `DestinationRule` teilen alle Pods hinter
  `backend` anhand des Labels `version` in zwei **Subsets** (`extern`,
  `intern`) auf.
- Ein `VirtualService` matcht pro Request auf `sourceNamespace` — den
  Kubernetes-Namespace, in dem der **aufrufende** Pod läuft — und routet je
  nach Match auf das passende Subset.
- **Wichtig:** Die Routing-Entscheidung wird am **ausgehenden** Envoy-Sidecar
  des *Clients* getroffen (nicht erst beim Server). Der Client muss daher
  selbst Teil des Meshes sein (Sidecar-Injection aktiv), sonst greift die
  Regel nicht und der Request landet per Kubernetes-Service-Load-Balancing
  zufällig auf einem der beiden Subsets.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster

## Installation

```bash
./install.sh                          # Standard-Namespace: namespace-routing-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Legt neben dem Haupt-Namespace `<namespace>` zusätzlich zwei Client-Namespaces
`<namespace>-extern` und `<namespace>-intern` an (alle drei mit Label
`istio-injection: enabled`). Existiert einer der drei Namespaces bereits, wird
dessen `namespace.yaml` übersprungen (kein erneutes Anlegen/Überschreiben).

Führt intern aus (Platzhalter `${NAMESPACE}`, `${EXTERN_NS}`, `${INTERN_NS}`
in den Manifesten werden per `sed` ersetzt):

```bash
kubectl get namespace <namespace>                                  # Existenzprüfung je Namespace
sed "..." manifests/00-namespace.yaml         | kubectl apply -f -  # nur falls <namespace> neu
sed "..." manifests/01-namespace-extern.yaml  | kubectl apply -f -  # nur falls <namespace>-extern neu
sed "..." manifests/02-namespace-intern.yaml  | kubectl apply -f -  # nur falls <namespace>-intern neu

sed "..." manifests/05-backend-extern-configmap.yaml | kubectl apply -f -
sed "..." manifests/06-backend-intern-configmap.yaml | kubectl apply -f -
sed "..." manifests/10-backend-extern-deployment.yaml | kubectl apply -f -
sed "..." manifests/11-backend-intern-deployment.yaml | kubectl apply -f -
sed "..." manifests/12-backend-service.yaml | kubectl apply -f -
sed "..." manifests/20-destinationrule-backend.yaml | kubectl apply -f -
sed "..." manifests/21-virtualservice-backend.yaml | kubectl apply -f -

kubectl -n <namespace> rollout status deployment/backend-extern --timeout=120s
kubectl -n <namespace> rollout status deployment/backend-intern --timeout=120s
kubectl -n <namespace> get deploy,svc,destinationrule,virtualservice
```

**Hinweis:** Werden bereits existierende Namespaces verwendet, müssen alle
drei (Haupt- und beide Client-Namespaces) das Label `istio-injection: enabled`
tragen, da sowohl der Server als auch beide Clients Teil des Meshes sein
müssen, damit die `sourceNamespace`-Regel greifen kann.

## Test ausführen

```bash
./run.sh                         # Standard-Namespace: namespace-routing-demo
./run.sh mein-namespace          # eigenen Namespace verwenden
```

Startet nacheinander je einen Test-Client-Pod in `<namespace>-extern` und
`<namespace>-intern`, die beide denselben Request gegen
`http://backend.<namespace>.svc.cluster.local` absetzen, und gibt die
jeweilige Antwort aus.

Führt intern aus (Platzhalter aus `test/client-pod.yaml` werden per `sed`
ersetzt, je einmal pro Client-Namespace):

```bash
sed -e "s|\${POD_NAME}|<generierter-name>|g" \
    -e "s|\${NAMESPACE}|<namespace>-extern|g" \
    -e "s|\${TARGET}|http://backend.<namespace>.svc.cluster.local|g" \
    test/client-pod.yaml | kubectl apply -f -

kubectl -n <namespace>-extern wait --for=jsonpath='{.status.phase}'=Succeeded pod/<generierter-name> --timeout=90s
kubectl -n <namespace>-extern logs <generierter-name> -c client
kubectl -n <namespace>-extern delete pod <generierter-name>
# ... analog für <namespace>-intern
```

Erwartete Ausgabe:

```
=== Request aus Namespace namespace-routing-demo-extern an http://backend.namespace-routing-demo.svc.cluster.local ===
Antwort vom EXTERN-Pod (version=extern)

=== Request aus Namespace namespace-routing-demo-intern an http://backend.namespace-routing-demo.svc.cluster.local ===
Antwort vom INTERN-Pod (version=intern)
```

Der Test-Client-Pod läuft **mit** Istio-Sidecar (im Gegensatz zu den
Lasttest-Pods in `ratelimits/`/`ratelimitService/`), da die Routing-Regel am
Client-seitigen Envoy ausgewertet wird. Da der Pod dadurch nach Ende von
`curl` sonst nicht terminieren würde (der `istio-proxy`-Container läuft
weiter), ruft der Client-Container anschließend
`http://localhost:15020/quitquitquit` auf, um den Sidecar aktiv zu beenden —
erst danach erreicht der Pod die Phase `Succeeded`.

## Troubleshooting

Kommt bei **beiden** Clients dieselbe Antwort zurück (z.B. immer die vom
`extern`-Pod, unabhängig vom aufrufenden Namespace): meist eine der
folgenden Ursachen.

- **Sidecar fehlt beim Client.** `sourceNamespace`-Matching greift nur, wenn
  der aufrufende Pod selbst Teil des Meshes ist. Prüfen mit:
  ```bash
  kubectl -n <namespace>-extern get pod <pod-name> -o jsonpath='{.spec.containers[*].name}'
  ```
  Es müssen zwei Container erscheinen (`client` und `istio-proxy`).

- **Zu kurz nach dem Anlegen des Namespaces/Pods getestet.** Istiod braucht
  nach dem Erstellen eines neuen Client-Namespaces bzw. Client-Pods kurz Zeit,
  bis dessen Pod-IP im Service-Registry allen Sidecars per xDS bekannt ist.
  Ein zu früher Testlauf direkt nach `install.sh` kann daher noch auf die
  Fallback-Regel treffen. Kurz warten und `./run.sh` erneut ausführen.

- **`DestinationRule`/`VirtualService` falscher Namespace.** Beide müssen im
  selben Namespace wie der `backend`-Service liegen (hier `<namespace>`),
  damit sie ohne `exportTo`/Cross-Namespace-Konfiguration automatisch für
  Aufrufer aus anderen Namespaces sichtbar sind.

Aktueller Zustand aller relevanten Ressourcen:

```bash
kubectl -n <namespace> get virtualservice backend -o yaml
kubectl -n <namespace> get destinationrule backend -o yaml
istioctl proxy-config route <client-pod> -n <namespace>-extern -o json
```

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: namespace-routing-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt nur die von diesem Usecase angelegten Ressourcen (VirtualService,
DestinationRule, Deployments, Service, ConfigMaps) — alle drei Namespaces
selbst bleiben bestehen. Zum vollständigen Entfernen:

```bash
kubectl delete namespace <namespace> <namespace>-extern <namespace>-intern
```
