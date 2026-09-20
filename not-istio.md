# Wann man Istio NICHT einsetzen sollte

Dieses Repo zeigt anhand von Usecases, wofür sich Istio gut eignet. Genauso
wichtig ist aber die Gegenfrage: Wann lohnt sich der Aufwand nicht? Dieses
Dokument fasst Argumente zusammen, warum Istio in bestimmten
Situationen die falsche Wahl ist.

## 1. Zu wenige Services / zu kleines Platform-Team

Ein Service Mesh amortisiert sich erst ab einer gewissen Anzahl an Services
und Teams, die sich nicht mehr gegenseitig auf Zuruf abstimmen können.

- Unter ca. 30–50 Services und einem kleinen (1–2 Personen) Platform-Team
  lohnt sich der Betriebsaufwand meist nicht. Timeouts/Retries lassen sich
  noch im Anwendungscode selbst durchsetzen.
- Als Faustregel gilt laut mehreren Quellen: Mesh nur einführen, wenn
  mindestens zwei von drei Fragen mit "Ja" beantwortet werden:
  1. Ist mTLS zwischen Services regulatorisch/durch Security-Review
     vorgeschrieben (nicht nur "nice to have")?
  2. Gibt es 50+ Services über mehrere Teams, die eine einheitliche
     Policy-Durchsetzung brauchen, ohne sich auf Implementierung im
     Anwendungscode jedes Teams verlassen zu können?
  3. Wird Cluster-übergreifendes, identitätsbasiertes Routing benötigt, das
     ein einfaches Regional-Gateway + Service-Registry nicht mehr leisten
     kann?
- Reine Observability ist **kein** ausreichender Grund für ein Mesh —
  L7-Telemetrie am Proxy zeigt nicht, welchen User-Request ein Aufruf bedient
  hat. Dafür reicht verteiltes Tracing (OpenTelemetry) ohne Mesh.

## 2. Monolithen oder VM-artige Workloads in Containern

Wenn Workloads containerisiert wurden, ohne in echte, feingranulare
Microservices aufgeteilt zu sein (Container quasi als VM-Ersatz), bringt ein
Mesh keinen Mehrwert. Einfache API-Gateway-Funktionen des Orchestrators
(Kubernetes/OpenShift) reichen dann aus.

## 3. Performance-kritische Workloads

Jeder Sidecar-Hop kostet zusätzliche Latenz (Client → Sidecar → Sidecar →
Server statt direkt). Je nach Quelle werden 2–10 ms pro Hop genannt, bei
tiefen Call-Chains kumulativ spürbar. Für Multimedia-Streaming,
Hochfrequenz-Trading oder Realtime-Gaming kann das bereits zu viel sein.
Zusätzlich beansprucht jeder Envoy-Sidecar spürbar CPU/RAM (Größenordnung
50–150 MB RAM, ~0,1–0,2 vCPU pro Pod), bei hunderten Pods relevante
Infrastrukturkosten rein fürs Networking.

## 4. Operative Komplexität und fehlendes Know-how

- Istio hat eine große CRD-Oberfläche, teils Breaking Changes zwischen
  Minor-Versionen und erfordert Envoy-spezifisches Debugging-Wissen
  (Envoy-Fehlercodes, `istioctl proxy-config`, EnvoyFilter-Syntax) statt
  Standard-Linux-Bordmitteln.
- Ohne dediziertes Platform-Team, das dieses Wissen aufbaut und pflegt, wird
  jede Störung zum Blackbox-Problem. Der Betriebsaufwand übersteigt schnell
  den Nutzen, gerade bei Teams mit wenigen Services.
- Ingress/externer Traffic bleibt ein Streitpunkt: oft wird zusätzlich ein
  klassischer Ingress-Controller benötigt, weil das Mesh-Gateway allein nicht
  alle Anforderungen abdeckt.

## 5. Wenn es speziellere Alternativen gibt

- **mTLS/Verschlüsselung:** Kernel-Level-Verschlüsselung (z. B. Cilium mit
  WireGuard) oder Verschlüsselung im DB-Treiber statt Sidecar-Interception.
- **Observability:** APM-Bibliotheken/OpenTelemetry liefern reichhaltigere,
  anwendungsbezogene Insights als reine Proxy-Telemetrie.
- **Traffic-Routing/Canary:** Lässt sich oft am Ingress-Controller lösen,
  statt Komplexität auf jeden Pod zu verteilen.
- **Sidecarless/eBPF:** Istio Ambient Mode oder Cilium (eBPF-basiert) bieten
  mesh-ähnliche Fähigkeiten (mTLS, L4/L7-Policies) mit deutlich weniger
  Sidecar-Overhead — relevant, wenn primär der Ressourcenverbrauch das
  Problem ist, nicht der Funktionsumfang von Istio.

## Groblinie / Entscheidungshilfe

| Situation | Empfehlung |
|---|---|
| < 30 Services, kleines Platform-Team, kein mTLS-Zwang | Kein Mesh — OpenTelemetry, Ingress-Envoy, cert-manager reichen |
| 30–100 Services, dediziertes Platform-Team, eine der drei Fragen "Ja" | Leichtgewichtige Alternative (z. B. Linkerd) statt Istio |
| 100+ Services, mehrere Cluster, mind. 2 der drei Fragen "Ja" | Istio (idealerweise Ambient Mode) — Aufwand bewusst tragen |
| Bereits Cilium als CNI im Einsatz | Cilium Service Mesh statt zusätzlichem Sidecar-Mesh prüfen |
| Monolith / grob geschnittene Services | Kein Mesh — Orchestrator-Bordmittel reichen |
| Latenzkritische Workloads (Trading, Streaming, Realtime-Gaming) | Sidecar-Overhead vorher explizit messen, ggf. Ambient Mode/eBPF |
