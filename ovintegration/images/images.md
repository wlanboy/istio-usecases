# Container-Images fuer eine Offline-Istio-Installation

Das Setup in diesem Ordner laeuft im Sidecar-Modus mit `istio-cni`, `istiod` und Ingress-Gateway,
installiert ueber die Helm-Charts `base`, `cni`, `istiod` und `gateway`. Dafuer braucht man drei Images:

| Image         | Verwendet von                                                                 |
|---------------|-------------------------------------------------------------------------------|
| `pilot`       | `istiod` (Control Plane)                                                      |
| `proxyv2`     | Sidecars in allen App-Pods und das Ingress-Gateway. Der Gateway-Chart setzt `image: auto`, der Injector ersetzt das durch `proxyv2` |
| `install-cni` | `istio-cni-node`-DaemonSet in `kube-system`                                   |

`ztunnel` braucht man nur fuer den Ambient-Modus. Der Chart `istio/base` enthaelt nur CRDs und
Rollen, aber keine Images.

Die Images liegen upstream unter `docker.io/istio`. Neuere Charts zeigen per Default auf
`registry.istio.io/release`, dort liegen dieselben Images.

## Variablen

```bash
export ISTIO_VERSION=1.30.5
export SRC_HUB=docker.io/istio
export DST_HUB=registry.example.internal/istio
```

## Pull, Tag, Push

Auf einem Rechner mit Internetzugang und Zugriff auf die private Registry:

```bash
docker login registry.example.internal

for img in pilot proxyv2 install-cni; do
  docker pull ${SRC_HUB}/${img}:${ISTIO_VERSION}
  docker tag  ${SRC_HUB}/${img}:${ISTIO_VERSION} ${DST_HUB}/${img}:${ISTIO_VERSION}
  docker push ${DST_HUB}/${img}:${ISTIO_VERSION}
done
```

Einzeln ausgeschrieben:

```bash
docker pull docker.io/istio/pilot:${ISTIO_VERSION}
docker tag  docker.io/istio/pilot:${ISTIO_VERSION} ${DST_HUB}/pilot:${ISTIO_VERSION}
docker push ${DST_HUB}/pilot:${ISTIO_VERSION}

docker pull docker.io/istio/proxyv2:${ISTIO_VERSION}
docker tag  docker.io/istio/proxyv2:${ISTIO_VERSION} ${DST_HUB}/proxyv2:${ISTIO_VERSION}
docker push ${DST_HUB}/proxyv2:${ISTIO_VERSION}

docker pull docker.io/istio/install-cni:${ISTIO_VERSION}
docker tag  docker.io/istio/install-cni:${ISTIO_VERSION} ${DST_HUB}/install-cni:${ISTIO_VERSION}
docker push ${DST_HUB}/install-cni:${ISTIO_VERSION}
```

Optional fuer Ambient:

```bash
docker pull docker.io/istio/ztunnel:${ISTIO_VERSION}
docker tag  docker.io/istio/ztunnel:${ISTIO_VERSION} ${DST_HUB}/ztunnel:${ISTIO_VERSION}
docker push ${DST_HUB}/ztunnel:${ISTIO_VERSION}
```

Fuer die Distroless-Variante haengt man `-distroless` an den Tag, also
`proxyv2:${ISTIO_VERSION}-distroless`, und setzt bei der Installation `global.variant=distroless`.

### Air Gap ohne direkte Verbindung zur Registry

`docker pull` wie oben, dann als Tarball uebertragen:

```bash
docker save -o istio-${ISTIO_VERSION}.tar \
  ${SRC_HUB}/pilot:${ISTIO_VERSION} \
  ${SRC_HUB}/proxyv2:${ISTIO_VERSION} \
  ${SRC_HUB}/install-cni:${ISTIO_VERSION}

# im Offline-Netz:
docker load -i istio-${ISTIO_VERSION}.tar
# danach tag + push wie oben
```

Docker speichert bei `pull` nur die Plattform des eigenen Rechners. Fuer Cluster mit anderer
Architektur, etwa arm64-Nodes, nimmt man `docker pull --platform linux/arm64` oder kopiert alle
Architekturen mit `skopeo copy --all docker://${SRC_HUB}/pilot:${ISTIO_VERSION} docker://${DST_HUB}/pilot:${ISTIO_VERSION}`.

## Helm-Charts offline

```bash
helm repo add istio https://istio-release.storage.googleapis.com/charts
helm repo update istio
for c in base cni istiod gateway; do
  helm pull istio/${c} --version ${ISTIO_VERSION}
done
```

Das ergibt `base-${ISTIO_VERSION}.tgz`, `cni-...`, `istiod-...` und `gateway-...`. Diese Dateien
ins Offline-Netz kopieren und bei `helm upgrade` statt `istio/<chart>` den Dateipfad angeben.

## Installation gegen die private Registry

`global.hub` und `global.tag` muessen bei `cni` und `istiod` gesetzt sein. Die Werte von `istiod`
gelten auch fuer injizierte Sidecars und fuer das Gateway, das seine Images ueber den Injector
bekommt.

```bash
helm upgrade --install istio-base ./base-${ISTIO_VERSION}.tgz -n istio-system \
  --set platform=openshift --wait

helm upgrade --install istio-cni ./cni-${ISTIO_VERSION}.tgz -n kube-system \
  --set platform=openshift \
  --set global.hub=${DST_HUB} \
  --set global.tag=${ISTIO_VERSION}

helm upgrade --install istiod ./istiod-${ISTIO_VERSION}.tgz -n istio-system \
  --set platform=openshift \
  --set pilot.cni.enabled=true \
  --set global.hub=${DST_HUB} \
  --set global.tag=${ISTIO_VERSION} --wait

helm upgrade --install istio-ingressgateway ./gateway-${ISTIO_VERSION}.tgz -n istio-system \
  --set platform=openshift \
  --set service.type=ClusterIP --wait
```

Braucht die Registry Credentials, kommt `--set global.imagePullSecrets[0]=<secret-name>` dazu.
Das Secret muss in `istio-system`, `kube-system` und in jedem App-Namespace mit Sidecar-Injection
existieren.

## Pruefen

```bash
oc get pods -A -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{range .spec.initContainers[*]}{.image}{"\n"}{end}{end}' \
  | grep istio | sort -u
```

Alle Eintraege muessen mit `${DST_HUB}` beginnen.
