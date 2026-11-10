{{- define "hd.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "hd.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "hd.labels" -}}
{{ include "hd.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* A base64 Secret value: the given value, else the one already in the cluster, else generated or an error. */}}
{{- define "hd.secretValue" -}}
{{- if .value -}}
{{- .value | b64enc -}}
{{- else if .existing -}}
{{- .existing -}}
{{- else if .generate -}}
{{- randAlphaNum 24 | b64enc -}}
{{- else -}}
{{- fail (printf "secrets.%s is required on first install" .name) -}}
{{- end -}}
{{- end -}}
