{{- define "bosgenesis-mop-execution-agent.secretEnv" -}}
{{- if .Values.external.postgresDsnSecret.name }}
- name: POSTGRES_DSN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.postgresDsnSecret.name }}
      key: {{ .Values.external.postgresDsnSecret.key }}
{{- end }}
{{- if .Values.external.databaseUrlSecret.name }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.databaseUrlSecret.name }}
      key: {{ .Values.external.databaseUrlSecret.key }}
{{- end }}
{{- if .Values.external.redisUrlSecret.name }}
- name: REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.redisUrlSecret.name }}
      key: {{ .Values.external.redisUrlSecret.key }}
{{- end }}
{{- if .Values.external.clickhouseDsnSecret.name }}
- name: CLICKHOUSE_DSN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.clickhouseDsnSecret.name }}
      key: {{ .Values.external.clickhouseDsnSecret.key }}
{{- end }}
{{- if .Values.external.qdrantUrlSecret.name }}
- name: QDRANT_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.qdrantUrlSecret.name }}
      key: {{ .Values.external.qdrantUrlSecret.key }}
{{- end }}
{{- if .Values.external.langfusePublicKeySecret.name }}
- name: LANGFUSE_PUBLIC_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.langfusePublicKeySecret.name }}
      key: {{ .Values.external.langfusePublicKeySecret.key }}
{{- end }}
{{- if .Values.external.langfuseSecretKeySecret.name }}
- name: LANGFUSE_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.langfuseSecretKeySecret.name }}
      key: {{ .Values.external.langfuseSecretKeySecret.key }}
{{- end }}
{{- if .Values.external.k8sInspectorApiKeySecret.name }}
- name: K8S_INSPECTOR_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.k8sInspectorApiKeySecret.name }}
      key: {{ .Values.external.k8sInspectorApiKeySecret.key }}
{{- end }}
{{- if .Values.external.helmManagerApiKeySecret.name }}
- name: HELM_MANAGER_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.external.helmManagerApiKeySecret.name }}
      key: {{ .Values.external.helmManagerApiKeySecret.key }}
{{- end }}
{{- end -}}
