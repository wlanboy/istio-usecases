# External Service TLS Origination Usecase

Demonstriert Istio **TLS Origination für Egress-Traffic**: eine `ServiceEntry`
registriert den externen Dienst `api.github.com` im Service-Mesh, eine
`DestinationRule` weist Istio an, Verbindungen zu Port 443 selbst per TLS
aufzubauen, und ein `VirtualService` schreibt Requests des App-Containers von
Port 80 auf Port 443 um. Der App-Container spricht dadurch nur Klartext-HTTP —
das komplette TLS-Handshake/Zertifikats-Handling zum externen Dienst übernimmt
der Envoy-Sidecar.

Das CA-Zertifikat, dem der Sidecar dabei vertrauen soll, ist **explizit
gepinnt** (`caCertificates`) statt sich auf ein eingebautes System-CA-Bundle
des Proxy-Images zu verlassen — relevant z. B. für Offline-/Air-Gap-Cluster,
die zur Laufzeit keine öffentlichen CA-Bundles nachladen können, aber auch
allgemein sicherer als "vertraue pauschal allen öffentlichen CAs".

## Architektur

```
Test-Client-Pod --curl http://api.github.com/ (Klartext, Port 80)--> Envoy-Sidecar (outbound)
                                                                        |
                                             VirtualService: Port 80 --> Port 443
                                                                        |
                          DestinationRule: Port 443 --> tls.mode=SIMPLE, caCertificates=<CA-Bundle>
                                                                        |
                                              CA-Bundle aus ConfigMap "github-ca-bundle",
                                              per Sidecar-Annotation NUR in istio-proxy gemountet
                                                                        |
                                                          TLS-Verbindung --> api.github.com:443
```

- Die `ServiceEntry` macht `api.github.com` im Mesh unter den Ports 80 (HTTP)
  und 443 (TLS) bekannt (`resolution: DNS`, `location: MESH_EXTERNAL`). Ohne
  sie könnten `DestinationRule`/`VirtualService` diesen externen Host nicht
  gezielt konfigurieren.
- Der `VirtualService` matcht Requests an Port 80 und routet sie auf denselben
  Host, aber Port 443.
- Die `DestinationRule` setzt für Port 443 `tls.mode: SIMPLE` und
  `caCertificates: /etc/istio-egress-certs/github-ca-bundle.pem` — der Sidecar
  authentifiziert den Server (api.github.com) über dieses gepinnte CA-Bundle
  und baut die TLS-Verbindung selbst auf.
- Das CA-Bundle (`manifests/05-configmap-github-ca.yaml`) enthält Intermediate-
  und Root-Zertifikat, die für `api.github.com` per
  `openssl s_client -connect api.github.com:443 -showcerts` heruntergeladen
  wurden. Über die Pod-Annotationen `sidecar.istio.io/userVolume` /
  `sidecar.istio.io/userVolumeMount` in `test/client-pod.yaml` wird die
  ConfigMap **nur** in den vom Sidecar-Injector erzeugten `istio-proxy`-
  Container gemountet (nicht in den App-Container `client`) — schließlich ist
  es der Sidecar, der die TLS-Verbindung aufbaut, nicht die App.
- **Wichtig:** Wie bei [`faultInjection/`](../faultInjection/readme.md) und
  [`namespaceRouting/`](../namespaceRouting/readme.md) wird die Regel am
  **ausgehenden** Envoy-Sidecar des *aufrufenden* Pods ausgewertet. Der
  Test-Client muss daher selbst Teil des Meshes sein (Sidecar-Injection
  aktiv), sonst geht der Request unverändert im Klartext auf Port 80 raus.

### Warum ist das Ergebnis eindeutig überprüfbar?

`api.github.com` antwortet auf einen direkten Klartext-Request an Port 80 nur
mit einem HTTP-301-Redirect auf `https://api.github.com/` — nicht mit den
echten API-Daten. Kommt beim Test-Client stattdessen HTTP 200 mit einem
echten JSON-Body (Feld `current_user_url`) an, beweist das, dass der Sidecar
die Verbindung tatsächlich per TLS auf Port 443 aufgebaut hat, obwohl der
App-Container selbst nur Port 80/Klartext angesprochen hat.

## Voraussetzungen

- laufender Kubernetes-Cluster mit installiertem Istio (Sidecar-Injection
  verfügbar)
- `kubectl` mit gültigem Kontext auf diesen Cluster
- ausgehender Internetzugriff des Clusters auf `api.github.com:443` (egress
  darf nicht durch NetworkPolicy/Firewall blockiert sein)

## Installation

```bash
./install.sh                          # Standard-Namespace: external-tls-demo
./install.sh mein-namespace           # eigenen Namespace verwenden
```

Existiert der angegebene Namespace bereits, wird `00-namespace.yaml`
übersprungen (kein erneutes Anlegen/Überschreiben) — nur ConfigMap,
ServiceEntry, DestinationRule und VirtualService werden in diesen bestehenden
Namespace appliziert.

Führt intern aus (Platzhalter `${NAMESPACE}` in den Manifesten werden per
`sed` durch den gewählten Namespace ersetzt):

```bash
kubectl get namespace <namespace>                       # Existenzprüfung
sed "s|\${NAMESPACE}|<namespace>|g" manifests/00-namespace.yaml | kubectl apply -f -   # nur falls Namespace neu
sed "s|\${NAMESPACE}|<namespace>|g" manifests/05-configmap-github-ca.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/10-serviceentry-api-github-com.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/20-destinationrule-api-github-com.yaml | kubectl apply -f -
sed "s|\${NAMESPACE}|<namespace>|g" manifests/21-virtualservice-api-github-com.yaml | kubectl apply -f -
kubectl -n <namespace> get configmap,serviceentry,destinationrule,virtualservice
```

**Hinweis:** Wird ein bereits existierender Namespace verwendet, muss dieser
das Label `istio-injection: enabled` tragen (bzw. Sidecar-Injection
anderweitig aktiviert haben), damit der Test-Client Teil des Meshes ist.

## Test ausführen

```bash
./run.sh                         # Standard-Namespace: external-tls-demo
./run.sh mein-namespace          # eigenen Namespace verwenden
```

Startet einen Test-Client-Pod, der `http://api.github.com/` im Klartext auf
Port 80 aufruft.

Führt intern aus (Platzhalter aus `test/client-pod.yaml` werden per `sed`
ersetzt):

```bash
sed -e "s|\${POD_NAME}|<generierter-name>|g" \
    -e "s|\${NAMESPACE}|<namespace>|g" \
    -e "s|\${TARGET}|http://api.github.com/|g" \
    test/client-pod.yaml | kubectl apply -f -

kubectl -n <namespace> wait --for=jsonpath='{.status.phase}'=Succeeded pod/<generierter-name> --timeout=90s
kubectl -n <namespace> logs <generierter-name> -c client
kubectl -n <namespace> delete pod <generierter-name>
```

Erwartete Ausgabe (gekürzt):

```
=== Klartext-HTTP-Request an http://api.github.com/ (Port 80, App-Container spricht kein TLS) ===
HTTP-Status: 200
--- Erste Zeile des Response-Bodys ---
{

==> TLS-Origination hat gegriffen: echte API-Antwort statt 301-Redirect erhalten.
```

Der Test-Client-Pod läuft **mit** Istio-Sidecar (analog zu
[`faultInjection/`](../faultInjection/readme.md)), da die
TLS-Origination-Regel am Client-seitigen Envoy ausgewertet wird. Als natives
Sidecar (Istio 1.27+, K8s 1.29+) beendet Kubelet den `istio-proxy`
automatisch, sobald der `client`-Container terminiert.

## Troubleshooting

Kommt beim Test-Client HTTP-Status **301** statt 200 zurück: meist eine der
folgenden Ursachen.

- **Sidecar fehlt beim Client.** Prüfen mit:
  ```bash
  kubectl -n <namespace> get pod <pod-name> -o jsonpath='{.spec.containers[*].name}'
  ```
  Es müssen zwei Container erscheinen (`client` und `istio-proxy`).

- **ServiceEntry/DestinationRule/VirtualService falscher Namespace oder noch
  nicht propagiert.** Istiod braucht nach dem Anwenden kurz Zeit, bis die
  Konfiguration per xDS beim Sidecar des Clients ankommt. Kurz warten und
  `./run.sh` erneut ausführen.

- **Egress blockiert.** Falls der Cluster ausgehenden Traffic per
  NetworkPolicy oder Istio `Sidecar`/`outboundTrafficPolicy: REGISTRY_ONLY`
  einschränkt, muss `api.github.com:443` explizit erlaubt sein.

Kommt der Test-Client-Pod dagegen gar nicht erst in die Phase `Succeeded`,
sondern hängt fest oder liefert einen Curl-Verbindungsfehler (kein HTTP-Status
mehr, sondern z. B. "Empty reply"/"connection reset"): meist ein Problem mit
dem gepinnten CA-Bundle.

- **ConfigMap fehlt oder ist im falschen Namespace.** Prüfen mit:
  ```bash
  kubectl -n <namespace> get configmap github-ca-bundle
  ```
- **Sidecar-Annotationen fehlen/falsch geschrieben.** Ohne korrektes
  `sidecar.istio.io/userVolume`/`userVolumeMount` (siehe
  `test/client-pod.yaml`) findet der Sidecar die Datei unter
  `/etc/istio-egress-certs/github-ca-bundle.pem` nicht — die `DestinationRule`
  referenziert dann einen nicht existierenden Pfad und der TLS-Handshake
  schlägt fehl. Prüfen, ob der Mount im `istio-proxy`-Container ankam:
  ```bash
  kubectl -n <namespace> exec <client-pod> -c istio-proxy -- ls -l /etc/istio-egress-certs
  ```
- **Zertifikat abgelaufen/rotiert.** GitHub kann die ausstellende CA wechseln.
  Mit `openssl s_client -connect api.github.com:443 -servername api.github.com
  -showcerts` erneut die aktuelle Kette abrufen und
  `manifests/05-configmap-github-ca.yaml` aktualisieren, falls sich Issuer
  oder Fingerprint geändert haben.

Aktueller Zustand der Regeln:

```bash
kubectl -n <namespace> get configmap,serviceentry,destinationrule,virtualservice -o yaml
istioctl proxy-config route <client-pod> -n <namespace> -o json
istioctl proxy-config cluster <client-pod> -n <namespace> --fqdn api.github.com -o json   # trafficSocketMatches/tlsContext zeigt den gepinnten caCertificates-Pfad
```

## Aufräumen

```bash
./uninstall.sh                   # Standard-Namespace: external-tls-demo
./uninstall.sh mein-namespace    # eigenen Namespace verwenden
```

Entfernt nur die von diesem Usecase angelegten Ressourcen (VirtualService,
DestinationRule, ServiceEntry) — der Namespace selbst bleibt bestehen. Zum
vollständigen Entfernen:

```bash
kubectl delete namespace <namespace>   # z.B. external-tls-demo
```
