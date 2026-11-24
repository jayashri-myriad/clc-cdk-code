FROM public.ecr.aws/lambda/python:3.12 AS base
RUN dnf install -y tar gzip findutils && dnf clean all

FROM base AS tools
COPY vendor/CLCServerCommandLineTools_26_0_7_64.sh /tmp/clc-installer.sh
RUN bash /tmp/clc-installer.sh -q -dir /opt/clc \
    && test -x /opt/clc/clcserver \
    && rm /tmp/clc-installer.sh

FROM base
COPY --from=tools /opt/clc /opt/clc
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt --target ${LAMBDA_TASK_ROOT}
COPY *.py ${LAMBDA_TASK_ROOT}/
ENV HOME=/tmp JAVA_TOOL_OPTIONS="-Djava.io.tmpdir=/tmp -Duser.home=/tmp"
CMD ["handlers.submit_handler"]
