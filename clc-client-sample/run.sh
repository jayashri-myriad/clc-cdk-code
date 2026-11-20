#!/usr/bin/env bash

set +x
set -e
: "${CLC_PASSWORD:?Set CLC_PASSWORD first}"
input=clc-client/input.local.json
read1=$(jq -er '.read1' "$input")
read2=$(jq -er '.read2' "$input")
destination=$(jq -er '.destination' "$input")

clc-client/.clc-install/clcserver \
  -S 127.0.0.1 -U root -W "$CLC_PASSWORD" -P 7778 \
  -L cgc-medium \
  -A "wf-myriad_genetics-qiaseq-dna-ultra-somatic--illumina----custom-bed-and-primers" \
  --reads-import-command ngs_import_illumina \
  --reads-select-files "$read1" \
  --reads-select-files "$read2" \
  --reads-paired-reads true \
  --destination-cloud "$destination"
