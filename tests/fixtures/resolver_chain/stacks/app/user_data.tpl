#!/bin/sh
ENV=${env}
%{ for p in ports }
open_port ${p}
%{ endfor }
