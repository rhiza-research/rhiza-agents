{{/*
Expand the name of the chart.
*/}}
{{- define "rhiza-agents.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
*/}}
{{- define "rhiza-agents.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "rhiza-agents.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "rhiza-agents.labels" -}}
helm.sh/chart: {{ include "rhiza-agents.chart" . }}
{{ include "rhiza-agents.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "rhiza-agents.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rhiza-agents.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
