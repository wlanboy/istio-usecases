# Multi-Cloud Mesh: lokalen Cluster mit Google Cloud (GKE) verbinden

Dieses Dokument beschreibt, wie zwei komplett getrennte Kubernetes-Cluster
verbunden werden: ein lokaler Cluster (z. B. zuhause/on-prem, hinter einem
gewöhnlichen Internet-Router) und ein GKE-Cluster in der Google Cloud. Ziel ist,
dass gezielt ausgewählte Dienste wechselseitig erreichbar sind, während alle
übrigen internen Dienste strikt isoliert bleiben. Nichts wird automatisch
sichtbar, nur was explizit freigegeben wurde. Diese Anforderung bestimmt die
Architekturentscheidung in diesem Dokument, siehe
[Topologie-Wahl](#topologie-wahl-multi-primary-primary-remote-oder-mesh-federation).

Es geht bewusst nicht um Befehle (`kubectl`/`istioctl`), sondern um
Architekturentscheidungen, Voraussetzungen und die Reihenfolge der Tätigkeiten vor
und während der eigentlichen Istio-Konfiguration. Die konkrete
Kommando-Ausführung unterscheidet sich je nach Istio-Version und
Federation-Controller und ist in den verlinkten Quellen dokumentiert.

## Architekturentscheidung: warum hier nur "Multi-Network" infrage kommt

Istio unterscheidet bei Mesh-übergreifenden Setups vier voneinander unabhängige
Achsen: Anzahl Cluster, Anzahl Netzwerke, Anzahl Kontrollebenen und Anzahl Meshes
([Deployment Models](https://istio.io/latest/docs/ops/deployment/deployment-models/)).
Für den Fall "lokaler Cluster + GKE" ist die Netzwerk-Frage bereits entschieden:

- Single-Network würde direkte Pod-zu-Pod-Erreichbarkeit über beide Cluster hinweg
  voraussetzen, also ein durchgeroutetes, flaches L3-Netz mit garantiert nicht
  überlappenden Pod-/Service-CIDRs
  ([Deployment Models](https://istio.io/latest/docs/ops/deployment/deployment-models/)).
  Das ist zwischen einem Heimnetz und einer GCP-VPC praktisch nie gegeben. Router
  im Heimnetz vergeben private RFC1918-Adressen ohne Rücksicht auf GCP, und die
  Kubernetes-Distribution zuhause (kind/minikube/k3s/microk8s) wählt ihre
  Pod-/Service-CIDRs unabhängig von der GKE-VPC.
- Multi-Network akzeptiert genau das: überlappende IP-Bereiche, kein direkter
  Pod-Zugriff über Cluster-Grenzen hinweg, stattdessen vermittelt ein dediziertes
  Gateway pro Cluster den Cross-Cluster-Verkehr
  ([Deployment Models](https://istio.io/latest/docs/ops/deployment/deployment-models/)).

Für dieses Szenario ist Multi-Network die einzige Wahl, unabhängig davon, ob am
Ende Multi-Primary, Primary-Remote oder Mesh Federation zum Einsatz kommt.

## Topologie-Wahl: Multi-Primary, Primary-Remote oder Mesh Federation

Unabhängig von der Netzwerk-Frage muss entschieden werden, wie viele
Kontrollebenen (istiod) das Mesh betreibt, und ob es überhaupt ein Mesh sein soll
oder zwei getrennte, die nur punktuell miteinander sprechen:

| | **Multi-Primary** | **Primary-Remote** | **Mesh Federation** |
|---|---|---|---|
| Kontrollebene | je Cluster ein eigener istiod, ein gemeinsames Mesh | ein istiod (in GCP), lokaler Cluster hat keine eigene Kontrollebene | je Cluster ein eigener istiod, zwei getrennte Meshes |
| Trust-Domain / Root-CA | gemeinsam (ein Root, zwei Intermediates) | gemeinsam (eine CA reicht) | frei wählbar: gemeinsamer Root oder getrennte Roots je Mesh |
| Service Discovery | automatisch über beide Cluster, jeder Service mit gleichem Namespace/Namen wird standardmäßig clusterübergreifend zusammengeführt | automatisch über beide Cluster | nicht automatisch, nur Services mit expliziter Export-Regel sind für die Gegenseite überhaupt sichtbar |
| API-Server-Zugriff nötig | beidseitig: beide Kontrollebenen müssen den jeweils anderen API-Server erreichen und dürfen dort per Remote Secret Objekte auflisten | einseitig: nur GCP muss den lokalen API-Server erreichen | keiner, die Federation-Controller sprechen ausschließlich per gRPC über das Federation-Gateway miteinander |
| Ausfallverhalten | jeder Cluster bleibt bei Verbindungsverlust für sich funktionsfähig | lokaler Cluster verliert bei Verbindungsverlust zu GCP die Konfigurationsversorgung | jeder Cluster bleibt vollständig funktionsfähig, auch administrativ komplett eigenständig |
| Referenz | [Install Multi-Primary on different networks](https://istio.io/latest/docs/setup/install/multicluster/multi-primary_multi-network/) | [Install Primary-Remote on different networks](https://istio.io/latest/docs/setup/install/multicluster/primary-remote_multi-network/) | [openshift-service-mesh/federation](https://github.com/openshift-service-mesh/federation), [Deployment Models](https://istio.io/latest/docs/ops/deployment/deployment-models/) |

### Warum das Service-Discovery-Verhalten hier den Ausschlag gibt

Bei Multi-Primary und Primary-Remote beobachtet jede Kontrollebene standardmäßig
alle Namespaces beider Cluster und führt Services mit identischem Namespace und
Namen automatisch clusterübergreifend zusammen. Das ist bewusst so gebaut, damit
Cross-Cluster-Failover ohne manuelle Konfiguration funktioniert. Einschränken
lässt sich das nur über `discoverySelectors` in der `MeshConfig`, einen
mesh-weiten, zentral gepflegten Schalter, der ganze Namespaces von der Beobachtung
ausschließt
([Discovery selectors in Istio 1.10](https://tetrate.io/blog/discovery-selectors),
[Istio-Blog: discovery selectors](https://istio.io/latest/blog/2021/discovery-selectors/)).
Das ist Namespace-, nicht Service-granular, und das Modell bleibt default-allow.
Ein neuer Namespace ohne passenden Selector, oder ein Service, dessen Name
versehentlich mit einem internen Dienst der Gegenseite kollidiert, wird
automatisch clusterübergreifend sichtbar. Genau dieses Szenario soll vermieden
werden: dann wären plötzlich viele interne Dienste einfach erreichbar.

Mesh Federation kehrt dieses Prinzip um. Es gibt keinen gemeinsamen
Service-Registry-Merge, jede Seite bleibt ein eigenständiges Mesh. Ein Service
wird für die Gegenseite ausschließlich sichtbar, wenn er explizit über eine
Export-Regel freigegeben wird (Schritt 7), default-deny, Service für Service oder
über eine bewusst gesetzte Label-Selektion, nie über einen ganzen Namespace
pauschal. Zusätzlich bestätigt die konsumierende Seite jeden angebotenen Service
noch einmal separat über eine Import-Regel (Schritt 8), die Sichtbarkeitsgrenze
wird also nie einseitig von nur einer Seite festgelegt. Als Nebeneffekt entfällt
außerdem der Remote-Secret-Mechanismus komplett: Kein Cluster bekommt jemals
Lesezugriff auf beliebige Objekte der Kubernetes-API des anderen, die
Kommunikation zwischen den Federation-Controllern läuft ausschließlich über das
Federation-Gateway per gRPC
([openshift-service-mesh/federation](https://github.com/openshift-service-mesh/federation)).

Empfehlung für dieses Szenario: Mesh Federation. Die explizite
Export-/Import-Konfiguration ist der Governance-Mechanismus, der verhindert, dass
jemals mehr als die bewusst freigegebenen Dienste über die Cluster-Grenze sichtbar
werden, ohne dabei auf zentrale, leicht zu vergessende `discoverySelectors`-Pflege
angewiesen zu sein. Multi-Primary und Primary-Remote bleiben oben zum Vergleich
stehen, sind für dieses Szenario aber bewusst nicht die empfohlene Wahl, weil ihr
Default-Allow-Verhalten dem gestellten Ziel widerspricht. Falls das Team der
OpenShift-Welt näher steht als reinem Upstream Istio: Red Hats OpenShift Service
Mesh bietet dasselbe Prinzip als nativ unterstütztes Feature
(`ServiceMeshPeer`/`ExportedServiceMeshSet`/`ImportedServiceSet`,
[Introducing OpenShift Service Mesh 2.1 - Federation Has Arrived](https://www.redhat.com/en/blog/introducing-openshift-service-mesh-2.1-federation-has-arrived)).
Für dieses Dokument (GKE und lokaler Nicht-OpenShift-Cluster) ist aber der
Upstream-Controller [openshift-service-mesh/federation](https://github.com/openshift-service-mesh/federation)
relevant, der trotz des Namens auf jedem Istio-Mesh läuft, nicht nur auf OpenShift.

## Das eigentliche Problem: Netzwerk-Konnektivität zwischen Heimnetz und GCP

Das ist der Teil, der bei "lokal + Cloud" (anders als bei zwei Cloud-Clustern)
tatsächlich schwierig ist, und er muss vor jeder Istio-Konfiguration gelöst sein.
Mit Mesh Federation reduziert sich das gegenüber Multi-Primary/Primary-Remote auf
genau eine Sache:

- Federation-Gateway. Der Datenverkehr der tatsächlich exportierten Services
  läuft im TLS-`AUTO_PASSTHROUGH`-Modus über dieses Gateway (SNI-Routing ohne
  TLS-Terminierung am Gateway, damit Ende-zu-Ende-mTLS erhalten bleibt).
  Zusätzlich trägt es den gRPC-Discovery-Kanal, über den sich die beiden
  Federation-Controller die Export-/Import-Konfiguration mitteilen
  ([Route across clusters with east-west gateways](https://docs.solo.io/gloo-mesh-enterprise/main/istio/eastwest/),
  [openshift-service-mesh/federation](https://github.com/openshift-service-mesh/federation)).

Anders als bei Multi-Primary/Primary-Remote muss dagegen kein
Kubernetes-API-Server der Gegenseite erreichbar sein. Das ist keine reine
Netzwerk-Erleichterung, sondern eine bewusste Sicherheitseigenschaft von
Federation: Selbst wenn Tunnel oder Gateway kompromittiert würden, gäbe es für die
Gegenseite keinen Weg, beliebige Objekte der eigenen Kubernetes-API aufzulisten.
Die Angriffsfläche bleibt auf die tatsächlich exportierten Service-Adressen
begrenzt.

Trotzdem bleibt eine private Netzwerkverbindung sinnvoll, als zusätzliche
Verteidigungsebene. Ein typischer Internet-Anschluss zuhause hat keine feste
öffentliche IP, oft sogar Carrier-Grade-NAT, und das Federation-Gateway sollte
nicht unnötig dem offenen Internet ausgesetzt werden, selbst wenn Export-Allowlist
und mTLS es bereits absichern. Ein klassisches Site-to-Site-VPN wie Google Cloud
VPN (auch HA VPN) setzt aber eine erreichbare, in der Regel statische öffentliche
IP auf der Gegenstelle voraus
([GKE with VPN – Networking options](https://sreeninet.wordpress.com/2019/08/11/gke-with-vpn-networking-options/)),
genau das ist im Heimnetz meist nicht gegeben.

Praktikable Lösung: ein WireGuard-Tunnel, der ausgehend vom Heimnetz zu einer
kleinen Compute-Engine-VM mit öffentlicher IP in der GCP-VPC aufgebaut wird. Da
die Verbindung vom lokalen Cluster initiiert wird, ist keine eingehende
Portfreigabe am Heimrouter nötig, die VM in GCP muss lediglich den
WireGuard-UDP-Port von außen annehmen
([Bridging Cloud and On-Premises: WireGuard VPN for Unified Kubernetes Networking](https://patel-aum.medium.com/bridging-cloud-and-on-premises-setting-up-wireguard-vpn-for-unified-kubernetes-networking-400d6a035bed)).
Für produktivere Umgebungen mit echter statischer IP auf der lokalen Seite ist
Cloud VPN die vom offiziellen GKE-Hybrid-Leitfaden empfohlene Variante, inklusive
Cloud-Router-Routenankündigung der beteiligten Subnetze in beide Richtungen
([Configure Hybrid Mesh – all GKE clusters must be in a VPC](https://cloud.google.com/service-mesh/v1.24/docs/operate-and-maintain/hybrid-mesh?hl=en)).

## Schritt für Schritt

### Schritt 1: Netzwerkbrücke aufbauen

Vor jeder Istio-Ressource steht der VPN-Tunnel zwischen Heimnetz und GCP-VPC
(WireGuard-VM oder Cloud VPN, siehe oben). Anschließend Routing einrichten, damit
die spätere Federation-Gateway-Adresse der Gegenseite über den Tunnel erreichbar
ist. Ein Zugriffspfad zum Kubernetes-API-Server der Gegenseite ist für die
Federation selbst nicht nötig (siehe oben). Falls für den eigenen Betrieb
trotzdem `kubectl`-Zugriff von außen gewünscht ist, ist das eine separate,
unabhängige Entscheidung.

### Schritt 2: Mesh-Identität pro Seite festlegen

Anders als bei Multi-Primary/Primary-Remote müssen `meshID`, `clusterName` und
`network` nicht zwischen den Seiten abgestimmt werden. Jedes Mesh wählt sie
unabhängig für sich, wie bei einer gewöhnlichen Einzelcluster-Installation.
Abgestimmt werden muss stattdessen die Peer-Beziehung selbst: ein stabiler Name
oder Hostname für die Federation-Gateway-Adresse jeder Seite sowie, abhängig von
der Entscheidung aus Schritt 3, der Trust-Domain-Name der Gegenseite.

### Schritt 3: Vertrauensbasis entscheiden

Zwei Optionen, beide mit Mesh Federation kompatibel:

- Gemeinsame Root-CA (ein Root, zwei Intermediates, wie im Multi-Primary-Modell).
  Einfacher umzusetzen und naheliegend, wenn beide Seiten ohnehin vom selben
  Betreiber verwaltet werden. Schwächt die Governance-Eigenschaft aus Schritt 7/8
  nicht, da die Sichtbarkeit über die Export-/Import-Regeln gesteuert wird, nicht
  über die CA-Struktur.
- Getrennte Root-CAs je Mesh mit explizitem Trust-Bundle-Austausch (nur das
  Root-Zertifikat der Gegenseite wird als zusätzlicher Trust-Anchor importiert,
  ausschließlich zur Validierung der Federation-Gateway-Verbindung). Stärkere
  Trennung, falls perspektivisch unterschiedliche Betreiber oder
  Compliance-Zonen entstehen sollen. Der verbreitete Federation-Controller setzt
  dafür entweder eine gemeinsame Root-CA oder SPIRE mit aktivierter
  Trust-Bundle-Federation voraus. Einen automatischen Bundle-Austausch zwischen
  unterschiedlichen, nicht-SPIRE-CAs liefert er nicht mit
  ([openshift-service-mesh/federation](https://github.com/openshift-service-mesh/federation)).

Für "ein Betreiber, zwei Umgebungen" (dieses Szenario) reicht Option 1. Details
zur gemeinsamen Root-CA: [How to Share Root CA Across Istio Clusters](https://oneuptime.com/blog/post/2026-02-24-how-to-share-root-ca-across-istio-clusters/view).

### Schritt 4: Kontrollebenen unabhängig installieren

Jeder Cluster bekommt eine vollständige, eigenständige Istio-Installation, wie
bei einem gewöhnlichen Einzelcluster-Setup. Kein `values.global.externalIstiod`,
kein aufeinander abgestimmter `meshID`, keine für Remote Secrets vorbereitete
Konfiguration nötig.

### Schritt 5: Federation-Gateway ausrollen und exponieren

In beiden Clustern ein dediziertes Ingress-Gateway für Federation-Verkehr
ausrollen. Anders als das East-West-Gateway bei Multi-Primary/Primary-Remote
leitet es nicht pauschal alle `*.local`-Hosts weiter, sondern nur den
gRPC-Discovery-Kanal der Federation-Controller sowie die tatsächlich exportierten
Services (Schritt 7).

In GCP läuft das Gateway hinter einem `Service` vom Typ `LoadBalancer`.
Empfehlenswert ist eine reservierte, statische externe IP-Adresse
([Static IP addresses für Ingress-Gateways](https://docs.cloud.google.com/apigee/docs/hybrid/v1.1/static-ip)),
dazu eine Firewall-Regel, beschränkt auf das Tunnel-Subnetz des VPN aus Schritt 1
statt `0.0.0.0/0`. Für die Health-Checks des Load Balancers zusätzlich Zugriff
aus den GCP-eigenen Health-Check-Bereichen `35.191.0.0/16` und `130.211.0.0/22`
erlauben
([LoadBalancer Service parameters: Health-Check-Firewallregel](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/service-load-balancer-parameters)).

Im lokalen Cluster wird das Gateway per `NodePort` (oder MetalLB, falls
vorhanden) exponiert und ist ausschließlich über die private VPN-Tunnel-Adresse
erreichbar, niemals über eine öffentliche Portfreigabe am Heimrouter.

### Schritt 6: Peer-Beziehung konfigurieren

Jede Seite hinterlegt eine Peer-Ressource für die Gegenseite: deren
Federation-Gateway-Adresse und, je nach Schritt 3, deren Trust-Info. An dieser
Stelle wird noch keine Service-Sichtbarkeit hergestellt, nur die technische
Beziehung zwischen den beiden Federation-Controllern.

```yaml
# schematisch. Konkrete Feldnamen haengen vom gewaehlten Federation-Controller ab,
# siehe github.com/openshift-service-mesh/federation
apiVersion: federation.istio-ecosystem.io/v1alpha1
kind: ServiceMeshPeer
metadata:
  name: gcp-mesh
  namespace: istio-system
spec:
  remote:
    addresses:
      - gcp-federation-gateway.internal   # ueber den VPN-Tunnel erreichbare Adresse
```

### Schritt 7: Export-Regeln definieren (die eigentliche Governance-Schicht)

Auf der exportierenden Seite wird pro Service (Namespace und Name, oder über
Label-Selektor) explizit festgelegt, was für die Gegenseite überhaupt sichtbar
wird. Alles, was hier nicht aufgeführt ist, bleibt für die Gegenseite unsichtbar.
Das ist der Mechanismus, der verhindert, dass plötzlich viele interne Dienste
einfach erreichbar wären.

```yaml
# schematisch
apiVersion: federation.istio-ecosystem.io/v1alpha1
kind: ExportedServiceSet
metadata:
  name: export-to-gcp
  namespace: istio-system
spec:
  peer: gcp-mesh
  rules:
    - type: NameSelector
      nameSelector:
        namespace: payments
        name: checkout-api   # nur dieser eine Service wird ueberhaupt exportiert
```

Diese Liste sollte wie eine Firewall-Regel behandelt werden: Änderungen
review-pflichtig, möglichst konkrete Service-Namen statt pauschaler
Label-Selektoren auf ganze Namespaces, regelmäßige Kontrolle, ob alle Einträge
noch gebraucht werden.

### Schritt 8: Import-Regeln auf der Konsumentenseite

Auf der konsumierenden Seite bestätigt eine zweite, unabhängige Regel, welche der
angebotenen Services tatsächlich als lokal erreichbarer Hostname eingebunden
werden. Die Sichtbarkeitsentscheidung liegt damit nie einseitig bei nur einer
Seite. Die exportierende Seite kann etwas anbieten, ohne dass es automatisch auf
der anderen Seite auftaucht, solange dort kein passender Import existiert.

### Schritt 9: Verifikation ohne `kubectl`/`istioctl`

- Ein exportierter Dienst wird von der Gegenseite aus erfolgreich erreicht
  (Positivtest).
- Ein bewusst nicht exportierter interner Dienst bleibt von der Gegenseite aus
  nachweislich unerreichbar. Dieser Negativtest gehört bei einem
  Governance-getriebenen Setup mit zur Verifikation, nicht nur der Positivtest.
- Kiali zeigt, falls installiert, bei Federation nur Kanten zu den tatsächlich
  exportierten und importierten Services, nicht den gesamten Mesh-Graphen beider
  Cluster zusammengeführt.
- Zugriffslogs und Metriken am Federation-Gateway: eingehende Verbindungen mit
  dem erwarteten SNI-Wert bestätigen, dass nur die erwarteten Hostnamen
  überhaupt angefragt werden.
- Der Zertifikatsvergleich ist nur relevant bei gemeinsamer Root-CA aus Schritt
  3: die Root-Zertifikats-Fingerprints beider Cluster müssen identisch sein.

## Betrieb und Sicherheit

- mTLS mesh-weit auf `STRICT` setzen (`PeerAuthentication`), damit
  Federation-Verkehr nicht versehentlich unverschlüsselt läuft.
- Firewall-Regeln so eng wie möglich fassen: Quellbereich immer das
  VPN-Tunnel-Subnetz, nie das offene Internet. Auch wenn Export-Allowlist und
  mTLS das Federation-Gateway bereits absichern, ist die Netzwerkgrenze eine
  zusätzliche, unabhängige Verteidigungsebene.
- Die Export-Liste aus Schritt 7 wie eine sicherheitsrelevante Konfiguration
  behandeln. Hier wird entschieden, was intern bleibt und was cloud-erreichbar
  wird, entsprechend sorgfältig pflegen und regelmäßig überprüfen.
- Istio- und Federation-Controller-Version im Blick behalten. Die Kopplung ist
  lockerer als bei Multi-Primary/Primary-Remote (kein gemeinsames xDS zwischen
  den Kontrollebenen), aber der Federation-Controller selbst hat eigene
  Versionsabhängigkeiten zur jeweiligen Istio-Version.
- VPN-Tunnel überwachen. Fällt er aus, sind ausschließlich die explizit
  federierten Services betroffen, beide Meshes bleiben davon unabhängig jederzeit
  vollständig funktionsfähig. Das ist der Vorteil gegenüber Primary-Remote. Bei
  dynamischer öffentlicher IP der GCP-VM zusätzlich einen stabilen DNS-Namen
  oder eine reservierte statische IP für den WireGuard-Endpunkt verwenden.
- Rotationsverfahren dokumentieren: bei gemeinsamer Root-CA (Schritt 3, Option 1)
  betrifft eine Rotation koordiniert beide Seiten, bei getrennten CAs (Option 2)
  ist die Rotation je Seite unabhängig, erfordert aber einen erneuten
  Trust-Bundle-Austausch.

## Zusammenfassung: Reihenfolge der Tätigkeiten

| # | Tätigkeit | Betrifft |
|---|---|---|
| 1 | VPN-Tunnel + Routing zum künftigen Federation-Gateway | Netzwerk |
| 2 | Mesh-Identität je Seite (unabhängig), Peer-Namen festlegen | beide Cluster |
| 3 | Vertrauensbasis entscheiden: gemeinsame Root-CA oder getrennt + Trust-Bundle | beide Cluster |
| 4 | Kontrollebenen unabhängig installieren | beide Cluster |
| 5 | Federation-Gateway ausrollen, statische IP + eng gefasste Firewall-Regel | beide Cluster |
| 6 | Peer-Beziehung konfigurieren | beide Cluster |
| 7 | Export-Regeln definieren (Governance-Schicht) | exportierende Seite |
| 8 | Import-Regeln definieren | konsumierende Seite |
| 9 | Verifikation inkl. Negativtest für nicht-exportierte Dienste | beide Cluster |

---
