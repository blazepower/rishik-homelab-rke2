{{- define "maybe-finance.runtimeCaInit" -}}
{{- if ne (int .Values.podSecurityContext.fsGroup) 1000 -}}
{{- fail "Sure public CA hardening requires the existing fsGroup 1000" -}}
{{- end -}}
- name: harden-public-ca
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command: ["ruby", "/ca-hardening/harden-ca.rb"]
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    readOnlyRootFilesystem: true
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]
  resources:
    requests:
      cpu: 25m
      memory: 64Mi
    limits:
      cpu: 250m
      memory: 256Mi
  volumeMounts:
    - name: sure-ca-tools
      mountPath: /ca-hardening
      readOnly: true
    - name: sure-ca-output
      mountPath: /ca-output
{{- end }}

{{- define "maybe-finance.runtimeCaMounts" -}}
- name: sure-ca-output
  mountPath: /etc/ssl/certs
  subPath: os-certs
  readOnly: true
- name: sure-ca-output
  mountPath: /usr/share/ca-certificates
  subPath: os-sources
  readOnly: true
- name: sure-ca-output
  mountPath: /usr/local/bundle/ruby/3.4.0/gems/aws-sdk-core-3.240.0/ca-bundle.crt
  subPath: aws.crt
  readOnly: true
- name: sure-ca-output
  mountPath: /usr/local/bundle/ruby/3.4.0/gems/skylight-6.0.4/lib/skylight/data/cacert.pem
  subPath: skylight.pem
  readOnly: true
- name: sure-runtime-tmp
  mountPath: /rails/tmp
- name: sure-runtime-log
  mountPath: /rails/log
{{- if not .persistentStorage }}
- name: sure-runtime-storage
  mountPath: /rails/storage
{{- end }}
{{- end }}

{{- define "maybe-finance.runtimeCaVolumes" -}}
- name: sure-ca-tools
  configMap:
    name: {{ required "Sure hardening ConfigMap name required" .root.Values.runtimeCaHardening.configMapName }}
- name: sure-ca-output
  emptyDir: {}
- name: sure-runtime-tmp
  emptyDir: {}
- name: sure-runtime-log
  emptyDir: {}
{{- if not .persistentStorage }}
- name: sure-runtime-storage
  emptyDir: {}
{{- end }}
{{- end }}

{{- define "maybe-finance.runtimeCaSecurity" -}}
{{- if .Values.runtimeCaHardening.enabled -}}
{{- toYaml (mergeOverwrite (deepCopy .Values.securityContext) (dict "readOnlyRootFilesystem" true)) -}}
{{- else -}}
{{- toYaml .Values.securityContext -}}
{{- end -}}
{{- end }}
