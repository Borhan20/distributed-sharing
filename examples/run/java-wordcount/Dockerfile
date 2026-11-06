# Stage 1: compile with the JDK (plain javac, no Maven needed for one file).
FROM eclipse-temurin:21-jdk-alpine AS build
WORKDIR /src
COPY WordCount.java .
RUN javac -d /classes WordCount.java

# Stage 2: run on the smaller JRE image, with only the classes and the text.
# The JVM takes its heap from the container's memory limit: 25% by default, 75% with this flag.
FROM eclipse-temurin:21-jre-alpine
COPY --from=build /classes /app
COPY text.txt /app/text.txt
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-cp", "/app", "WordCount"]
