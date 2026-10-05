# Traffic Mirroring mit Istio – Leitfaden für Entwickler

Hier steht, was beim Mirroring technisch passiert, wo die Fallen liegen und
wie man Mirroring zu einem Deployment hinzufügt, das schon läuft. Den
lauffähigen Demo-Aufbau mit nginx und logger beschreibt die
[readme.md](readme.md).

## Wofür Mirroring gedacht ist

Beim Mirroring, auch Shadow-Traffic genannt, schickt Envoy eine Kopie echter
Requests an einen zweiten Service. Der Aufrufer merkt davon nichts. Typische
Anwendungen:

- eine neue Version `v2` mit echtem Produktionstraffic testen, bevor sie
  echten Nutzern antwortet
- Requests mitschneiden, etwa zum Debuggen oder um die Antworten alter und
  neuer Implementierung zu vergleichen
- messen, wie sich eine neue Implementierung unter realer Last verhält

Für eine schrittweise Umstellung im Sinne eines Canary-Releases taugt
Mirroring nicht. Dafür gibt es gewichtetes Routing mit `weight` in der
`route`.

## Wie es funktioniert

```
Aufrufer-Pod
  └─ Envoy-Sidecar (outbound)  ← hier wird der VirtualService ausgewertet
       ├─ route  ──► service-a        (PRIMARY: Antwort geht an den Aufrufer)
       └─ mirror ──► service-a-shadow (Kopie, Antwort wird verworfen)
```

1. Der Aufrufer schickt einen Request an `service-a`.
2. Sein Envoy-Sidecar findet im `VirtualService` für `service-a` neben der
   `route` einen `mirror`-Eintrag.
3. Envoy leitet den Request ganz normal an `service-a` weiter und schickt
   parallel eine Kopie mit Methode, Pfad, Headern und Body an das
   Mirror-Ziel.
4. Der Aufrufer bekommt nur die Antwort von `service-a`. Die Antwort des
   Mirror-Ziels wirft Envoy weg, ohne auf sie zu warten. Ist das Mirror-Ziel
   langsam, liefert es Fehler oder ist es gar nicht erreichbar, ändert das
   nichts an Status, Body oder Latenz der eigentlichen Antwort.

Was das für Entwickler bedeutet:

- **Mirroring passiert beim Aufrufer.** Die Regel greift im Sidecar des
  aufrufenden Pods, nicht beim Ziel. Aufrufer ohne Sidecar erzeugen keine
  Kopie. Kommt der Traffic von außen über ein Ingress-Gateway, spiegelt das
  Gateway. Der `mirror` muss dann in dem `VirtualService` stehen, der unter
  `gateways` an das Gateway gebunden ist.
- **Envoy ändert den Host-Header.** Beim gespiegelten Request hängt Envoy
  `-shadow` an den `Host`- bzw. `:authority`-Header an. Aus
  `service-a.ns.svc.cluster.local` wird `service-a.ns.svc.cluster.local-shadow`.
  Daran erkennt der Shadow-Service, dass er eine Kopie bekommt. Wer im Code
  nach Host routet oder den Host validiert, muss das einplanen.
- **Die Kopie enthält alle Header.** Tokens, Cookies und personenbezogene
  Daten im Body landen beim Mirror-Ziel. Für das Ziel gelten deshalb dieselben
  Schutzanforderungen wie für den Original-Service, und es darf Tokens nicht
  im Klartext loggen. Der `logger` in diesem Usecase schwärzt sie aus genau
  diesem Grund.
- **Seiteneffekte passieren doppelt.** Das ist meiner Meinung nach die
  gefährlichste Falle. Ein gespiegelter `POST /orders` legt im
  Shadow-Service eine zweite Bestellung an, wenn er auf dieselbe Datenbank
  oder dieselben Downstream-Services zugreift. Dasselbe gilt für E-Mails,
  Zahlungen und Kafka-Nachrichten. Der Shadow-Service braucht deshalb eine
  eigene Datenbank bzw. Testumgebung, oder er unterdrückt schreibende Aufrufe,
  sobald der Host auf `-shadow` endet.
- **Mirroring gilt pro `http`-Route.** `mirror` steht innerhalb eines
  Eintrags unter `spec.http[]`. Hat ein `VirtualService` mehrere Routen, etwa
  per `match` auf Pfade, spiegelt Envoy nur die Routen mit `mirror`.
- **Das Mirror-Ziel bekommt echte Last.** Bei `mirrorPercentage: 100` sieht
  es dieselbe Request-Rate wie der Original-Service. Fehlen ihm die
  Ressourcen, laufen dort Timeouts auf. Der Aufrufer merkt nichts, in den
  Metriken des Ziels sieht man es aber.

## Die Felder im VirtualService

```yaml
spec:
  hosts:
    - service-a.my-ns.svc.cluster.local
  http:
    - route:
        - destination:
            host: service-a.my-ns.svc.cluster.local
      mirror:
        host: service-a-shadow.my-ns.svc.cluster.local   # Mirror-Ziel
        # subset: v2        # optional, braucht eine DestinationRule mit Subset
        # port:
        #   number: 8080    # nur nötig, wenn der Service mehrere Ports hat
      mirrorPercentage:
        value: 10.0         # Anteil der gespiegelten Requests in Prozent
```

| Feld | Bedeutung |
|---|---|
| `mirror.host` | Service, der die Kopie bekommt. Das Mesh muss ihn kennen, als Kubernetes-Service oder `ServiceEntry`. |
| `mirror.subset` | Optional. Spiegelt auf ein Subset aus einer `DestinationRule`, z. B. auf Pods mit Label `version: v2` hinter demselben Service. |
| `mirror.port.number` | Optional. Zielport, falls der Mirror-Service mehrere Ports hat. |
| `mirrorPercentage.value` | Anteil der Requests von 0.0 bis 100.0. Fehlt das Feld, spiegelt Envoy 100 %. |

Seit Istio 1.22 gibt es zusätzlich das Feld `mirrors`. Es nimmt eine Liste
und spiegelt an mehrere Ziele gleichzeitig, jedes mit eigenem `percentage`.
`mirror` und `mirrors` dürfen nicht in derselben Route stehen.

```yaml
      mirrors:
        - destination:
            host: service-a-shadow.my-ns.svc.cluster.local
          percentage:
            value: 10.0
        - destination:
            host: request-logger.my-ns.svc.cluster.local
          percentage:
            value: 100.0
```

## Mirroring zu einem bestehenden Deployment hinzufügen

In den Beispielen läuft `service-a` im Namespace `my-ns`. Andere Pods im Mesh
rufen ihn auf, und diese Requests sollen zusätzlich an ein Mirror-Ziel gehen.

### Schritt 1. Voraussetzungen prüfen

Aufrufer und Ziel brauchen einen Sidecar. Den Namespace prüfen, erwartet wird
`istio-injection=enabled` oder `istio.io/rev=<revision>`:

```bash
kubectl get namespace my-ns --show-labels
```

In den Aufrufer-Pods muss ein Container `istio-proxy` auftauchen:

```bash
kubectl -n <aufrufer-namespace> get pod <aufrufer-pod> -o jsonpath='{.spec.containers[*].name} {.spec.initContainers[*].name}'
```

Istio muss den Service-Port als HTTP erkennen. Das klappt über einen Portnamen
mit Präfix `http`, also `http` oder `http-web`, oder über
`appProtocol: http`. Sonst behandelt Envoy den Traffic als TCP, und TCP lässt
sich nicht spiegeln.

```bash
kubectl -n my-ns get service service-a -o jsonpath='{range .spec.ports[*]}{.name} {.appProtocol} {.port}{"\n"}{end}'
```

### Schritt 2. Mirror-Ziel bereitstellen

Dafür gibt es zwei Varianten. Ich würde Variante A nehmen, solange nichts
dagegen spricht. Sie hat weniger bewegliche Teile.

**Variante A, eigener Service für das Mirror-Ziel.** Ein neues Deployment
`service-a-shadow` bekommt einen eigenen Service. Dessen Selector darf nicht
auf die Pods von `service-a` passen, sonst bekommt das Ziel auch regulären
Traffic.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: service-a-shadow
  namespace: my-ns
spec:
  selector:
    app: service-a-shadow
  ports:
    - name: http
      port: 80
      targetPort: 8080
```

**Variante B, Subset hinter dem bestehenden Service.** Die neue Version läuft
mit den Labels `app: service-a` und `version: v2`. Dafür braucht es eine
`DestinationRule` mit Subsets, und die normale Route muss explizit auf `v1`
zeigen. Fehlt das, verteilt der Service den regulären Traffic per Round-Robin
auch auf die `v2`-Pods.

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: service-a
  namespace: my-ns
spec:
  host: service-a.my-ns.svc.cluster.local
  subsets:
    - name: v1
      labels:
        version: v1
    - name: v2
      labels:
        version: v2
```

Gibt es für `service-a` schon eine `DestinationRule`, etwa für mTLS oder den
Connection-Pool, gehören die Subsets dort hinein. Eine zweite
`DestinationRule` für denselben Host führt zu Konflikten.

### Schritt 3. Vorhandene VirtualServices prüfen

```bash
kubectl get virtualservice -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}: {.spec.hosts}{"\n"}{end}' | grep service-a
```

Das Ergebnis entscheidet, wie es weitergeht.

#### Fall 1. Für `service-a` gibt es noch keinen VirtualService

Einen neuen anlegen. Die `route` zeigt auf den Service selbst, damit sich am
normalen Routing nichts ändert:

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: service-a
  namespace: my-ns
spec:
  hosts:
    - service-a.my-ns.svc.cluster.local
  http:
    - route:
        - destination:
            host: service-a.my-ns.svc.cluster.local
            # subset: v1    # nur bei Variante B
      mirror:
        host: service-a-shadow.my-ns.svc.cluster.local
        # host: service-a.my-ns.svc.cluster.local   # Variante B
        # subset: v2                                # Variante B
      mirrorPercentage:
        value: 10.0
```

```bash
kubectl apply -f service-a-virtualservice.yaml
```

#### Fall 2. Für `service-a` gibt es schon einen VirtualService

Hier keinen zweiten VirtualService für denselben Host anlegen. Istio führt
mehrere VirtualServices mit demselben Host für Sidecar-Traffic nicht
zuverlässig zusammen. Einer gewinnt, und die Regeln des anderen fehlen
einfach. Stattdessen `mirror` und `mirrorPercentage` im bestehenden
VirtualService ergänzen, in jeder `http`-Route, die gespiegelt werden soll.

Vorher:

```yaml
  http:
    - match:
        - uri:
            prefix: /api
      route:
        - destination:
            host: service-a.my-ns.svc.cluster.local
      timeout: 5s
```

Nachher:

```yaml
  http:
    - match:
        - uri:
            prefix: /api
      route:
        - destination:
            host: service-a.my-ns.svc.cluster.local
      timeout: 5s
      mirror:
        host: service-a-shadow.my-ns.svc.cluster.local
      mirrorPercentage:
        value: 10.0
```

Verwaltet Helm, Kustomize oder ein GitOps-Tool wie Argo CD oder Flux den
VirtualService, gehört die Änderung ins Manifest. Einen direkten
`kubectl patch` überschreibt der nächste Sync wieder. Für einen schnellen Test
im Cluster reicht der Patch trotzdem. Das Beispiel ändert die erste Route mit
Index `0`:

```bash
kubectl -n my-ns patch virtualservice service-a --type=json -p='[
  {"op": "add", "path": "/spec/http/0/mirror", "value": {"host": "service-a-shadow.my-ns.svc.cluster.local"}},
  {"op": "add", "path": "/spec/http/0/mirrorPercentage", "value": {"value": 10.0}}
]'
```

Den Index der richtigen Route vorher nachsehen:

```bash
kubectl -n my-ns get virtualservice service-a -o jsonpath='{range .spec.http[*]}{.name} {.match}{"\n"}{end}'
```

#### Fall 3. Der Traffic kommt über ein Ingress-Gateway

Dann gehört der `mirror` in den VirtualService, der unter `gateways` das
Gateway referenziert. Ein Mirror im Mesh-internen VirtualService wirkt nur
für Aufrufe aus Sidecars, nicht für Requests vom Gateway.

### Schritt 4. Mit wenig Traffic starten

Mit 1 bis 10 % in `mirrorPercentage` anfangen. Erhöhen erst, wenn das
Mirror-Ziel stabil läuft und keine unerwünschten Seiteneffekte zeigt.

### Schritt 5. Prüfen, ob es wirkt

Ob die Regel beim Sidecar des Aufrufers ankommt, zeigt die
Route-Konfiguration. Dort muss ein Eintrag `requestMirrorPolicies`
auftauchen:

```bash
istioctl proxy-config route <aufrufer-pod> -n <aufrufer-namespace> -o json | grep -A5 requestMirrorPolicies
```

Konfigurationsfehler findet `istioctl analyze`:

```bash
istioctl analyze -n my-ns
```

Ob Requests beim Mirror-Ziel ankommen, zeigen die Logs von Anwendung und
Sidecar:

```bash
kubectl -n my-ns logs deployment/service-a-shadow --since=5m
kubectl -n my-ns logs deployment/service-a-shadow -c istio-proxy --since=5m
```

Gespiegelte Requests erkennt man im Access-Log des `istio-proxy` am Suffix
`-shadow` in der Authority.

### Mirroring wieder entfernen

`mirror` und `mirrorPercentage` aus dem Manifest löschen und neu anwenden.
Wer per Patch hinzugefügt hat, entfernt die Felder ebenso:

```bash
kubectl -n my-ns patch virtualservice service-a --type=json -p='[
  {"op": "remove", "path": "/spec/http/0/mirror"},
  {"op": "remove", "path": "/spec/http/0/mirrorPercentage"}
]'
```

Wer den VirtualService nur für das Mirroring angelegt hat, wie in Fall 1,
kann ihn komplett löschen. Danach routet wieder der Kubernetes-Service allein:

```bash
kubectl -n my-ns delete virtualservice service-a
```

## Checkliste

- [ ] Aufrufer und Ziel haben einen Sidecar, bei externem Traffic spiegelt das Gateway
- [ ] Istio erkennt den Service-Port als HTTP, per Portname `http*` oder `appProtocol`
- [ ] Das Mirror-Ziel bekommt keinen regulären Traffic, dank eigenem Selector oder `v1`-Route
- [ ] Das Mirror-Ziel schreibt nicht in produktive Datenbanken oder Queues und löst keine echten Seiteneffekte aus
- [ ] Das Mirror-Ziel loggt Tokens und personenbezogene Daten nicht im Klartext
- [ ] Es gibt nur einen VirtualService pro Host, mit `mirror` in jeder gewünschten `http`-Route
- [ ] Der Start erfolgt mit kleinem `mirrorPercentage`
- [ ] `istioctl proxy-config route` zeigt `requestMirrorPolicies`
