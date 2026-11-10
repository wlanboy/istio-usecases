# gRPC mit Istio im Sidecar-Modus

gRPC läuft über HTTP/2. Envoy kennt das Protokoll und behandelt jeden gRPC-Call als
eigenen HTTP/2-Request. Damit stehen im Sidecar-Modus und an den Gateways alle
L7-Funktionen von Istio auch für gRPC zur Verfügung. Ambient Mode ist hier nicht
beschrieben.

## Unterstützung je Betriebsart

| Betriebsart | gRPC | L7-Features | Load-Balancing |
|---|---|---|---|
| Sidecar | ja | vollständig | pro Request |
| Ingress-/Egress-Gateway | ja | vollständig, per `VirtualService` oder Gateway-API-`GRPCRoute` | pro Request |
| Proxyless gRPC, xDS in der gRPC-Bibliothek | eingeschränkt, experimentell | Teilmenge: Routing, LB, mTLS | pro Request im Client |

Mit L7-Features sind hier gemeint: Routing nach Service und Methode, Retries, Timeouts, Fault
Injection, AuthorizationPolicy auf Methodenebene und Metriken mit gRPC-Status.

## Load-Balancing pro Call

gRPC-Clients halten eine einzige HTTP/2-Verbindung lange offen und schicken alle
Calls darüber. `kube-proxy` verteilt nur Verbindungen, ohne Mesh landen deshalb alle
Requests eines Clients auf demselben Pod. Neue Pods nach einem Scale-up bekommen
keinen Traffic, bis der Client neu verbindet.

Der Envoy-Sidecar terminiert die HTTP/2-Verbindung des Clients und verteilt jeden
einzelnen Call auf die Endpoints. Neue Pods bekommen sofort Traffic, ohne Änderung
an der Anwendung.

## Protokoll deklarieren

Envoy muss wissen, dass auf dem Port gRPC läuft. Dafür den Service-Port `grpc` oder
`grpc-<suffix>` nennen oder `appProtocol: grpc` setzen:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: greeter
spec:
  selector:
    app: greeter
  ports:
    - name: grpc
      port: 9090
      targetPort: 9090
      appProtocol: grpc
```

Fehlt die Angabe, versucht Istio das Protokoll per Protocol Sniffing zu erkennen.
Das klappt nicht in jedem Fall und kostet beim Verbindungsaufbau Zeit.

Ein häufiger Fehler ist ein Port mit dem Namen `http`. Istio spricht dann zum
Upstream HTTP/1.1, und gRPC-Calls scheitern mit `upstream connect error` oder
`protocol error`. Abhilfe schafft ein umbenannter Port oder
`connectionPool.http.useClientProtocol: true` in der `DestinationRule`.

## Routing nach Service und Methode

Der gRPC-Pfad hat die Form `/<package>.<Service>/<Methode>`. Ein `VirtualService`
matcht darauf wie auf jeden HTTP-Pfad:

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: greeter
spec:
  hosts:
    - greeter
  http:
    - match:
        - uri:
            prefix: /helloworld.Greeter/
          headers:
            x-version:
              exact: v2
      route:
        - destination:
            host: greeter
            subset: v2
    - route:
        - destination:
            host: greeter
            subset: v1
      timeout: 5s
      retries:
        attempts: 3
        perTryTimeout: 2s
        retryOn: unavailable,cancelled,deadline-exceeded,resource-exhausted
```

- Header-Matches greifen auf gRPC-Metadaten, die der Client mitschickt.
- `retryOn` kennt die gRPC-Statuscodes `cancelled`, `deadline-exceeded`, `internal`,
  `resource-exhausted` und `unavailable`. Ohne eigene Angabe nutzt Istio
  `connect-failure,refused-stream,unavailable,cancelled,retriable-status-codes` mit
  2 Versuchen.
- Nur idempotente Methoden retryen lassen. Bei Streaming-Calls wiederholt Envoy
  nur, solange noch keine Antwort zum Client geflossen ist.
- Die Deadline des Clients steht im Header `grpc-timeout` und gilt unabhängig vom
  `timeout` im `VirtualService`. Es greift der kürzere Wert.

## Fault Injection

Statt eines HTTP-Status lässt sich ein gRPC-Status injizieren:

```yaml
    fault:
      abort:
        grpcStatus: UNAVAILABLE
        percentage:
          value: 10
```

## AuthorizationPolicy auf Methodenebene

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: greeter-allow
spec:
  selector:
    matchLabels:
      app: greeter
  action: ALLOW
  rules:
    - from:
        - source:
            principals: ["cluster.local/ns/demo/sa/client"]
      to:
        - operation:
            paths: ["/helloworld.Greeter/SayHello"]
```

gRPC nutzt immer `POST`. Eine Regel auf `methods: ["GET"]` trifft also nie.
Abgelehnte Calls bekommen `PERMISSION_DENIED` zurück, also gRPC-Status 7.

## Telemetrie

- `istio_requests_total` trägt zusätzlich das Label `grpc_response_status`. Der
  HTTP-Status ist bei gRPC fast immer 200, auch bei fehlgeschlagenen Calls.
  Fehlerquoten deshalb über `grpc_response_status != "0"` auswerten.
- Für Streaming-Calls zählen `istio_request_messages_total` und
  `istio_response_messages_total` die einzelnen Nachrichten.
- Im Access-Log steht der gRPC-Status in `%GRPC_STATUS%`.

## Health-Probes

gRPC-Probes von Kubernetes wie `livenessProbe.grpc` schreibt der Sidecar-Injector wie
HTTP-Probes auf den `pilot-agent` um. Sie funktionieren deshalb auch mit
`STRICT`-mTLS.

## Ingress-Gateway

Der Gateway-Server braucht das Protokoll `HTTP2`, `GRPC` oder `HTTPS`. Bei `HTTPS`
handelt ALPN `h2` aus. Beispiel:

```yaml
apiVersion: networking.istio.io/v1
kind: Gateway
metadata:
  name: grpc-gw
spec:
  selector:
    istio: ingressgateway
  servers:
    - port:
        number: 443
        name: https-grpc
        protocol: HTTPS
      tls:
        mode: SIMPLE
        credentialName: grpc-cert
      hosts:
        - grpc.example.local
```

Mit der Gateway API ist `GRPCRoute` die passende Ressource. Sie matcht direkt auf
`service` und `method` statt auf den URI-Pfad.

Für gRPC-Web aus dem Browser hängt Istio den Envoy-Filter `grpc_web` in die
HTTP-Filterketten. Die Übersetzung nach gRPC übernimmt also der Proxy.

## Proxyless gRPC

Die gRPC-Bibliothek holt sich Konfiguration direkt per xDS von istiod, ohne
Envoy-Sidecar. Aktiviert wird das mit der Annotation
`inject.istio.io/templates: grpc-agent`, der Client nutzt `xds:///<service>:<port>`
als Target. Der Funktionsumfang ist eine Teilmenge der Sidecar-Variante und hängt
von Sprache und Version der gRPC-Bibliothek ab. Der Modus ist experimentell und
für Produktion nur nach eigener Prüfung geeignet.
