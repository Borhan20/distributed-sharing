# R from the r-base image. Extra packages would go here (RUN R -e 'install.packages(...)'):
# jobs have no network while they run.
FROM r-base:4.4.2
COPY bootstrap.R /app/bootstrap.R
ENTRYPOINT ["Rscript", "/app/bootstrap.R"]
