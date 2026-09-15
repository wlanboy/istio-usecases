# Egress Lockdown Usecase

Demonstriert, wie man einen Namespace per Istio so absperrt, dass Workloads
darin **keine einzige Ressource außerhalb des Clusters** mehr aufrufen
können — unabhängig davon, welche Domain, IP oder welches Protokoll die
Anwendung anspricht. Kommunikation innerhalb des Clusters (andere
Kubernetes-Services) bleibt davon unberührt.

Kernstück ist eine `Sidecar`-Ressource, die für den gesamten Namespace
(kein `workloadSelector` gesetzt) `outboundTrafficPolicy.mode: REGISTRY_ONLY`
setzt: der Envoy-Sidecar lässt ausgehenden Traffic nur noch zu Hosts durch,
die Istio in seiner **Service-Registry** kennt. Das sind automatisch alle
Kubernetes-Services des Clusters sowie explizit per `ServiceEntry`
freigegebene externe Hosts. Standardmäßig ist damit **alles Externe**
verboten — ähnlich wie [`externalServiceTls/`](../externalServiceTls/readme.md)
gezielt einen externen Host per `ServiceEntry` freigibt, erlaubt dieser
Usecase bewusst genau **eine** Ausnahme: den internen Forgejo-Server
`git.gmk.lan:3300`, den Workloads im Namespace z. B. für `git clone`/CI
benötigen. Jeder andere Cluster-externe Host bleibt gesperrt.

## Architektur

```
                              Namespace "egress-lockdown-demo"
                              (Sidecar: outboundTrafficPolicy=REGISTRY_ONLY)
                                         |
Test-Client-Pod --curl http://nginx.<ns>.svc.cluster.local/ --> Envoy-Sidecar (outbound)
                                         |
                        Ziel ist Kubernetes-Service (in Registry bekannt)
                                         |
                                        nginx-Pod  ==> HTTP 200 (erlaubt)


Test-Client-Pod --curl http://github.com/ ----------------------> Envoy-Sidecar (outbound)
                                         |
                      Ziel ist NICHT in der Registry (kein ServiceEntry)
                                         |
                                  BlackHoleCluster  ==> Verbindung wird verworfen (blockiert)


Test-Client-Pod --curl https://git.gmk.lan:3300/ ----------------> Envoy-Sidecar (outbound)
                                         |
            Ziel ist explizit per ServiceEntry freigegeben (TLS-Passthrough per SNI)
                                         |
                                git.gmk.lan:3300  ==> erreichbar (erlaubte Ausnahme)
```

- Die `Sidecar`-Ressource (`manifests/20-sidecar-registry-only.yaml`) wird
  ohne `workloadSelector` im Namespace angelegt und gilt damit für **alle**
  Workloads darin, die keinen eigenen, spezifischeren `Sidecar` haben.
- `outboundTrafficPolicy.mode: REGISTRY_ONLY` ersetzt das Standardverhalten
  `ALLOW_ANY` (jeder ausgehende Traffic zu unbekannten Hosts wird
  durchgereicht). Envoy kennt danach nur noch Cluster-Ziele (automatisch
  registrierte Kubernetes-Services) sowie per `ServiceEntry` explizit
  freigegebene externe Hosts.
- Ohne passenden `ServiceEntry` landet jeder Versuch, einen Cluster-externen
  Host anzusprechen, im `BlackHoleCluster` von Envoy — die Verbindung wird
  sofort verworfen, statt ins Internet durchgereicht zu werden.
- Die einzige Ausnahme ist `manifests/21-serviceentry-forgejo.yaml`: ein
  `ServiceEntry` registriert `git.gmk.lan:3300` (Protokoll `TLS`,
  `resolution: DNS`, `location: MESH_EXTERNAL`) in der Service-Registry.
  Damit erkennt `REGISTRY_ONLY` dieses eine Ziel als bekannt und lässt es
  durch. Da Port 3300 bereits HTTPS spricht, terminiert der Sidecar die
  TLS-Verbindung nicht selbst (keine `DestinationRule`/TLS-Origination wie
  in [`externalServiceTls/`](../externalServiceTls/readme.md) nötig) —
  Envoy leitet die verschlüsselte Verbindung nur anhand der SNI weiter
  (TLS-Passthrough).
- Ein `nginx`-Deployment/-Service im selben Namespace dient als
  In-Cluster-Ziel, um zu beweisen, dass Registry-Only **nur** Cluster-externe
  Aufrufe betrifft, aber Aufrufe zu anderen Services im Cluster weiterhin
  funktionieren.

### Warum ist das Ergebnis eindeutig überprüfbar?

Der Test-Client ruft im selben Lauf drei Ziele auf: den internen
`nginx`-Service (`http://nginx.<namespace>.svc.cluster.local/`), den
gesperrten externen `http://github.com/` und den per `ServiceEntry`
freigegebenen `https://git.gmk.lan:3300/`. Liefern interner Aufruf und
`git.gmk.lan` je einen HTTP-Status, während `github.com` fehlschlägt (kein
HTTP-Status, sondern ein Verbindungsfehler wie `Connection reset`/leere
Antwort), beweist das, dass der Sidecar ausschließlich nicht freigegebenen
Cluster-externen Traffic sperrt — weder den gesamten Netzwerkzugriff des
Pods noch die bewusst erlaubte Ausnahme.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection
  verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster

## Installation

```bash
./install.sh                          # Standard-Namespace: egress-lockdown-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml`
übersprungen (kein erneutes Anlegen/Überschreiben) — nur ConfigMap,
Deployment, Service, die `Sidecar`-Ressource und der `ServiceEntry` für
`git.gmk.lan` werden in diesen bestehenden Namespace appliziert.

Führt intern aus (Platzhalter `${NAMESPACE}` in den Manifesten werden per
`sed` durch den gewählten Namespace ersetzt):

```bash
kubectl get namespace <namespace>                       # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml | kubectl apply -f -   # nur falls Namespace neu
sed "s|\${NAMESPACE}|<namespace>|g" manifests/05-nginx-configmap.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/10-nginx-deployment.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/11-nginx-service.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/20-sidecar-registry-only.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/21-serviceentry-forgejo.yaml | kubectl apply -f -
kubectl -n <namespace> rollout status deployment/nginx
kubectl -n <namespace> get deployment,service,sidecar,serviceentry
```

**Hinweis:** `git.gmk.lan:3300` ist die interne Forgejo-Instanz dieses
Setups. Für ein eigenes freizugebenes Ziel `manifests/21-serviceentry-forgejo.yaml`
(Host, Port, Protokoll) entsprechend anpassen oder die Datei löschen, wenn
keine Ausnahme benötigt wird.

**Hinweis:** Wird ein bereits existierender Namespace verwendet, muss dieser
das Label `istio-injection: enabled` tragen (bzw. Sidecar-Injection
anderweitig aktiviert haben), damit sowohl `nginx` als auch der Test-Client
Teil des Meshes sind — ohne Sidecar greift `REGISTRY_ONLY` gar nicht erst.

## Test ausführen

```bash
./run.sh                         # Standard-Namespace: egress-lockdown-demo
./run.sh mein-namespace          # eigenen Namespace verwenden
```

Startet einen Test-Client-Pod, der nacheinander den internen `nginx`-Service,
den gesperrten `http://github.com/` und den per `ServiceEntry` erlaubten
`https://git.gmk.lan:3300/` aufruft.

Führt intern aus (Platzhalter aus `test/client-pod.yaml` werden per `sed`
ersetzt):

```bash
sed -e "s|\${POD_NAME}|<generierter-name>|g" \
    -e "s|\${NAMESPACE}|<namespace>|g" \
    -e "s|\${INTERNAL_TARGET}|http://nginx.<namespace>.svc.cluster.local/|g" \
    -e "s|\${EXTERNAL_TARGET}|http://github.com/|g" \
    -e "s|\${ALLOWED_EXTERNAL_TARGET}|https://git.gmk.lan:3300/|g" \
    test/client-pod.yaml | kubectl apply -f -

kubectl -n <namespace> wait --for=jsonpath='{.status.phase}'=Succeeded pod/<generierter-name> --timeout=90s
kubectl -n <namespace> logs <generierter-name> -c client
kubectl -n <namespace> delete pod <generierter-name>
```

Erwartete Ausgabe (gekürzt):

```
=== Test 1: interner Aufruf im Cluster (http://nginx.egress-lockdown-demo.svc.cluster.local/) ===
Status: 200

=== Test 2: externer Aufruf ausserhalb des Clusters (http://github.com/) ===
Status/Fehler: FAIL

=== Test 3: per ServiceEntry erlaubter externer Aufruf (https://git.gmk.lan:3300/) ===
Status/Fehler: 200

==> Interner Aufruf erfolgreich (erwartet: Registry-Only blockiert nur Cluster-externe Hosts).
==> Externer Aufruf blockiert (erwartet: Egress-Lockdown greift, kein ServiceEntry fuer dieses Ziel).
==> Erlaubter externer Aufruf erfolgreich (erwartet: ServiceEntry gibt genau dieses Ziel frei, Status 200).
```

Der genaue Status von Test 3 hängt von der Forgejo-Instanz ab (z. B. auch
ein Redirect); entscheidend ist nur, dass **irgendein** HTTP-Status statt
eines Verbindungsfehlers zurückkommt.

Der Test-Client-Pod läuft **mit** Istio-Sidecar, da `REGISTRY_ONLY` am
Client-seitigen Envoy ausgewertet wird — läuft der Pod ohne Sidecar (z. B.
weil der Namespace keine Injection aktiviert hat), geht der externe Aufruf
unverändert durch, ganz ohne dass Istio ihn sieht.

## Troubleshooting

Kommt beim externen Test **kein** Fehler zurück, sondern ein HTTP-Status
(z. B. 301 von `github.com`, dem Redirect auf HTTPS): Registry-Only greift
nicht.

- **Sidecar-Ressource fehlt oder falscher Namespace.** Prüfen mit:
  ```bash
  kubectl -n <namespace> get sidecar egress-lockdown -o yaml
  ```
- **Test-Client hat keinen Sidecar.** Prüfen mit:
  ```bash
  kubectl -n <namespace> get pod <pod-name> -o jsonpath='{.spec.containers[*].name}'
  ```
  Es müssen zwei Container erscheinen (`client` und `istio-proxy`).
- **Konfiguration noch nicht propagiert.** Istiod braucht nach dem Anwenden
  kurz Zeit, bis `REGISTRY_ONLY` per xDS beim Sidecar ankommt. Kurz warten
  und `./run.sh` erneut ausführen.
- **Ein anderer, breiterer `Sidecar` (mesh- oder namespace-weit) überschreibt
  die Policy**, oder eine `ServiceEntry` in `istio-system`/im Mesh gibt den
  Ziel-Host global frei. Prüfen mit:
  ```bash
  kubectl get sidecar -A
  kubectl get serviceentry -A
  ```

Schlägt dagegen **Test 3** (`git.gmk.lan:3300`) fehl, obwohl `github.com`
korrekt blockiert wird:

- **`ServiceEntry` fehlt, falscher Namespace oder falscher Host/Port.**
  Prüfen mit:
  ```bash
  kubectl -n <namespace> get serviceentry forgejo-git-gmk-lan -o yaml
  ```
- **`git.gmk.lan` ist vom Cluster aus nicht auflösbar oder nicht
  erreichbar** (privater/interner DNS-Name, z. B. nur im Heimnetz gültig).
  Das ist ein Netzwerk-/DNS-Problem außerhalb von Istio, nicht der
  `Sidecar`-Policy. Prüfen mit:
  ```bash
  kubectl -n <namespace> exec <client-pod> -c client -- nslookup git.gmk.lan
  ```
- **Konfiguration noch nicht propagiert**, siehe oben — kurz warten und
  `./run.sh` erneut ausführen.

Schlägt dagegen bereits der **interne** Aufruf (`nginx`-Service) fehl:

- **nginx-Deployment nicht bereit.** Prüfen mit:
  ```bash
  kubectl -n <namespace> get pods -l app=nginx
  ```
- **`REGISTRY_ONLY` zu restriktiv konfiguriert** (z. B. durch einen
  zusätzlichen, falsch gesetzten `egress.hosts`-Filter in der
  `Sidecar`-Ressource, der auch den eigenen Namespace ausschließt). In
  diesem Usecase ist bewusst **kein** `egress.hosts` gesetzt, damit nur
  `outboundTrafficPolicy` greift und die Sichtbarkeit auf alle
  In-Cluster-Services erhalten bleibt.

Aktueller Zustand der Regeln:

```bash
kubectl -n <namespace> get sidecar -o yaml
istioctl proxy-config cluster <client-pod> -n <namespace> | grep -i blackhole
istioctl proxy-config listener <client-pod> -n <namespace>
```

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: egress-lockdown-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt nur die von diesem Usecase angelegten Ressourcen (Sidecar,
ServiceEntry, nginx-Deployment/-Service/-ConfigMap) — der Namespace selbst
bleibt bestehen. Zum vollständigen Entfernen:

```bash
kubectl delete namespace <namespace>   # z.B. egress-lockdown-demo
```
