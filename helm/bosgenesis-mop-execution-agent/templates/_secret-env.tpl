{{- define "bosgenesis-mop-execution-agent.secretEnv" -}}
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
{{- end -}}
