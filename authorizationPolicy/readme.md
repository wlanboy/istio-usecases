# Authorization Policy Usecase

Demonstriert Istio **AuthorizationPolicy mit Allow/Deny anhand der
Source-Workload-Identity**: zwei Clients (`client-a`, `client-b`), jeder mit
eigenem `ServiceAccount`, rufen denselben Service (`backend`) auf. Per
Default dürfen beide zugreifen. `incident.yaml` simuliert einen
Sicherheitsvorfall — angewendet per `kubectl apply -f incident.yaml` sperrt
sie gezielt genau `client-b` aus, ohne `client-a` oder `backend` selbst
anzufassen.

## Architektur

```
Namespace <ns>:
  client-a (SA: client-a) --curl--> backend.<ns> (SA: backend)
  client-b (SA: client-b) --curl--> backend.<ns> (SA: backend)

Ohne incident.yaml:        AuthorizationPolicy "block-client-b-incident"
  client-a --> 200 OK        selector: app=backend
  client-b --> 200 OK        action: DENY
                              from.source.namespaces: [<ns>]
Mit incident.yaml:          from.source.principals:
  client-a --> 200 OK          [cluster.local/ns/<ns>/sa/client-b]
  client-b --> 403 Forbidden
```

- `backend`, `client-a` und `client-b` laufen als eigene Deployments, jedes
  mit **eigenem ServiceAccount**. Da alle drei Teil des Meshes sind
  (Sidecar-Injection aktiv), baut Istio zwischen ihnen automatisch mTLS auf —
  ohne dass dafür extra ein `PeerAuthentication`-Objekt nötig wäre.
- Über das mTLS-Client-Zertifikat kennt der Envoy-Sidecar von `backend` die
  **SPIFFE-Identity** des Aufrufers: `cluster.local/ns/<namespace>/sa/<service-account>`
  — genau die Kombination aus Namespace und ServiceAccount, die
  `incident.yaml` in `from.source.principals` referenziert.
- Ohne AuthorizationPolicy gilt der Istio-Default **Allow-All**. `incident.yaml`
  fügt eine gezielte `DENY`-Regel für `client-b` hinzu; alle anderen Aufrufer
  (inkl. `client-a`) bleiben von dieser Regel unberührt und weiterhin erlaubt.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster

## Installation

```bash
./install.sh                          # Standard-Namespace: authz-policy-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml`
übersprungen (kein erneutes Anlegen/Überschreiben).

Führt intern aus (Platzhalter `${NAMESPACE}` in den Manifesten werden per
`sed` durch den gewählten Namespace ersetzt):

```bash
kubectl get namespace <namespace>                                        # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml | kubectl apply -f -   # nur falls Namespace neu

sed "..." manifests/05-backend-serviceaccount.yaml  | kubectl apply -f -
sed "..." manifests/06-backend-configmap.yaml       | kubectl apply -f -
sed "..." manifests/10-backend-deployment.yaml      | kubectl apply -f -
sed "..." manifests/11-backend-service.yaml         | kubectl apply -f -
sed "..." manifests/20-client-a-serviceaccount.yaml | kubectl apply -f -
sed "..." manifests/21-client-a-deployment.yaml     | kubectl apply -f -
sed "..." manifests/22-client-b-serviceaccount.yaml | kubectl apply -f -
sed "..." manifests/23-client-b-deployment.yaml     | kubectl apply -f -

kubectl -n <namespace> rollout status deployment/backend --timeout=120s
kubectl -n <namespace> rollout status deployment/client-a --timeout=120s
kubectl -n <namespace> rollout status deployment/client-b --timeout=120s
kubectl -n <namespace> get deploy,svc,serviceaccount
```

Direkt nach der Installation ist **noch keine** `AuthorizationPolicy` aktiv —
beide Clients dürfen zugreifen.

## Test ausführen

```bash
./run.sh                         # Standard-Namespace: authz-policy-demo
./run.sh mein-namespace          # eigenen Namespace verwenden
```

Führt intern je einen `kubectl exec` in die Pods von `client-a` und
`client-b` aus und curlt von dort `http://backend.<namespace>.svc.cluster.local`:

```bash
kubectl -n <namespace> exec <client-a-pod> -c client -- \
  curl -s -o /dev/null -w 'HTTP-Status: %{http_code}\n' http://backend.<namespace>.svc.cluster.local
# ... analog für client-b
```

Erwartete Ausgabe **vor** dem Vorfall:

```
==> Request von 'client-a' (ServiceAccount 'client-a', Pod client-a-xxx) an http://backend.authz-policy-demo.svc.cluster.local
HTTP-Status: 200

==> Request von 'client-b' (ServiceAccount 'client-b', Pod client-b-xxx) an http://backend.authz-policy-demo.svc.cluster.local
HTTP-Status: 200

==> incident.yaml ist NICHT aktiv: beide Clients erwarten 200.
```

## Vorfall simulieren

```bash
kubectl apply -f incident.yaml
./run.sh
```

Erwartete Ausgabe **nach** dem Vorfall:

```
==> Request von 'client-a' ... HTTP-Status: 200
==> Request von 'client-b' ... HTTP-Status: 403

==> incident.yaml ist aktiv: 'client-a' erwartet 200, 'client-b' erwartet 403.
```

`client-a` ist unverändert erreichbar — nur `client-b` wird anhand seiner
Identity (Namespace `authz-policy-demo` + ServiceAccount `client-b`)
geblockt.

Bei abweichendem Namespace (`install.sh mein-namespace`) vorher
`authz-policy-demo` in `incident.yaml` ersetzen:

```bash
sed 's/authz-policy-demo/mein-namespace/g' incident.yaml | kubectl apply -f -
```

Vorfall aufheben:

```bash
kubectl delete -f incident.yaml
# bzw. bei abweichendem Namespace:
kubectl -n mein-namespace delete authorizationpolicy block-client-b-incident
```

## Troubleshooting

- **`client-b` bekommt weiterhin 200 statt 403.** Meist eine der folgenden
  Ursachen:
  - `incident.yaml` wurde im falschen Namespace angewendet (siehe oben) —
    prüfen mit `kubectl -n <namespace> get authorizationpolicy`.
  - Zu kurz nach `kubectl apply -f incident.yaml` getestet: Istiod braucht
    kurz Zeit, bis die Regel per xDS beim Sidecar von `backend` ankommt.
  - Der `ServiceAccount` von `client-b` weicht vom in `incident.yaml`
    referenzierten Principal ab (z.B. weil `manifests/22-*`/`23-*` manuell
    angepasst wurden).

- **Auch `client-a` bekommt 403.** `selector.matchLabels` in `incident.yaml`
  trifft mehr Pods als gedacht, oder `from.source.principals` wurde zu
  allgemein formuliert. Aktuellen Zustand der Regel prüfen:
  ```bash
  kubectl -n <namespace> get authorizationpolicy block-client-b-incident -o yaml
  istioctl x authz check <backend-pod> -n <namespace>
  ```

- **Wichtiger Hinweis zu mTLS:** `source.principals` kann nur ausgewertet
  werden, wenn der Request tatsächlich per mTLS beim Sidecar von `backend`
  ankommt (hier automatisch der Fall, da beide Seiten im Mesh sind). Ruft ein
  Client `backend` **ohne** Sidecar (also im Klartext) auf, bleibt die
  Identity unbekannt und die `DENY`-Regel greift nicht — das Deny ist dann
  wirkungslos statt "fail closed". Für echte Enforcement-Garantien zusätzlich
  mTLS mit `PeerAuthentication` (`mode: STRICT`) erzwingen.

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: authz-policy-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt zuerst eine ggf. noch aktive `incident.yaml`-Policy, danach die
übrigen Ressourcen dieses Usecases (Deployments, Services, ServiceAccounts,
ConfigMap) — der Namespace selbst bleibt bestehen. Zum vollständigen
Entfernen:

```bash
kubectl delete namespace <namespace>   # z.B. authz-policy-demo
```
