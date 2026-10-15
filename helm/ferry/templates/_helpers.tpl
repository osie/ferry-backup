{{- define "ferry.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "ferry.labels" -}}
app.kubernetes.io/part-of: ferry
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "ferry.selector" -}}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/name: {{ printf "ferry-%s" .component }}
{{- end -}}

{{- define "ferry.componentLabels" -}}
app.kubernetes.io/part-of: ferry
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{ include "ferry.selector" . }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ferry.serviceAccount" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "ferry.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "ferry.cloudName" -}}
{{- required "cloud.name is required: it names this deployment in every point it writes" .Values.cloud.name -}}
{{- end -}}

{{- define "ferry.siteName" -}}
{{- default (include "ferry.cloudName" .) .Values.cloud.site -}}
{{- end -}}

{{- define "ferry.credentialsSecret" -}}
{{- default (printf "%s-credentials" (include "ferry.fullname" .)) .Values.credentials.existingSecret -}}
{{- end -}}

{{- define "ferry.databaseHost" -}}
{{- if .Values.database.embedded.enabled -}}
{{- printf "%s-mariadb.%s.svc" (include "ferry.fullname" .) .Release.Namespace -}}
{{- else -}}
{{- .Values.database.host -}}
{{- end -}}
{{- end -}}

{{- define "ferry.dsn" -}}
{{- printf "%s:$(FERRY_DB_PASSWORD)@tcp(%s:%v)/%s" .Values.database.user (include "ferry.databaseHost" .) .Values.database.port .Values.database.name -}}
{{- with .Values.database.params -}}?{{ . }}{{- end -}}
{{- end -}}

{{- define "ferry.adminDSN" -}}
{{- printf "%s:$(FERRY_DB_ADMIN_PASSWORD)@tcp(%s:%v)/" .Values.database.bootstrap.admin.user (include "ferry.databaseHost" .) .Values.database.port -}}
{{- with .Values.database.params -}}?{{ . }}{{- end -}}
{{- end -}}

{{- define "ferry.apiService" -}}
{{- printf "%s-api" (include "ferry.fullname" .) -}}
{{- end -}}

{{- define "ferry.moverApiService" -}}
{{- printf "%s-mover-api" (include "ferry.fullname" .) -}}
{{- end -}}

{{- define "ferry.managerReplicasService" -}}
{{- printf "%s-manager-replicas" (include "ferry.fullname" .) -}}
{{- end -}}

{{- define "ferry.internalUrl" -}}
{{- default (printf "http://%s.%s.svc.cluster.local:%v" (include "ferry.apiService" .) .Release.Namespace .Values.api.service.port) .Values.keystone.catalog.internalUrl -}}
{{- end -}}

{{- define "ferry.publicUrl" -}}
{{- if .Values.keystone.catalog.publicUrl -}}
{{- .Values.keystone.catalog.publicUrl -}}
{{- else if and .Values.api.ingress.enabled .Values.api.ingress.host -}}
{{- printf "%s://%s" (ternary "https" "http" (gt (len .Values.api.ingress.tls) 0)) .Values.api.ingress.host -}}
{{- else if and .Values.api.route.enabled .Values.api.route.host -}}
{{- printf "https://%s" .Values.api.route.host -}}
{{- else -}}
{{- include "ferry.internalUrl" . -}}
{{- end -}}
{{- end -}}

{{- define "ferry.moverName" -}}
{{- printf "%s-mover" (include "ferry.fullname" .) -}}
{{- end -}}

{{- define "ferry.managerClientCN" -}}
{{- printf "%s-manager-client" (include "ferry.fullname" .) -}}
{{- end -}}

{{- define "ferry.computeServerName" -}}
{{- printf "%s-compute" (include "ferry.fullname" .) -}}
{{- end -}}

{{- define "ferry.computeTLSServerName" -}}
{{- if .Values.compute.tlsServerName -}}
{{- .Values.compute.tlsServerName -}}
{{- else if or .Values.compute.daemonset .Values.compute.edpm.enabled -}}
{{- include "ferry.computeServerName" . -}}
{{- end -}}
{{- end -}}

{{- define "ferry.moverDrainSeconds" -}}
{{- $s := .Values.mover.drainTimeoutSeconds -}}
{{- if or (kindIs "invalid" $s) (le (int $s) 0) -}}90{{- else -}}{{ int $s }}{{- end -}}
{{- end -}}

{{- define "ferry.moverIdentities" -}}
{{- $out := list -}}
{{- range $i := until (int .Values.mover.replicas) -}}
{{- $out = append $out (printf "%s-%d" (include "ferry.moverName" $) $i) -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{- define "ferry.pkiSecret" -}}
{{- $root := .root -}}
{{- $name := .name -}}
{{- if eq $root.Values.pki.provider "existing" -}}
{{- $existing := $root.Values.pki.existing -}}
{{- if eq $name "ca" -}}{{ required "pki.existing.ca is required" $existing.ca }}
{{- else if eq $name "mover-api-server" -}}{{ required "pki.existing.moverApiServer is required" $existing.moverApiServer }}
{{- else if eq $name "manager-client" -}}{{ required "pki.existing.managerClient is required" $existing.managerClient }}
{{- else if eq $name "compute-server" -}}{{ required "pki.existing.computeServer is required" $existing.computeServer }}
{{- else -}}{{ $name }}
{{- end -}}
{{- else -}}
{{- printf "%s-%s" (include "ferry.fullname" $root) $name -}}
{{- end -}}
{{- end -}}

{{- define "ferry.image" -}}
image: {{ .image | quote }}
imagePullPolicy: {{ .root.Values.images.pullPolicy }}
{{- end -}}

{{- define "ferry.cephUsers" -}}
{{- $u := .Values.ceph.users -}}
{{- list $u.manager $u.mover $u.restore | toJson -}}
{{- end -}}

{{- define "ferry.keyringSecret" -}}
{{- default (printf "%s-ceph-keyrings" (include "ferry.fullname" .)) .Values.ceph.keyrings.existingSecret -}}
{{- end -}}

{{- define "ferry.readablePools" -}}
{{- $p := .Values.ceph.pools -}}
{{- concat $p.volumes $p.vms $p.images $p.ferry | uniq | toJson -}}
{{- end -}}
