FROM eclipse-temurin:21-jre
ARG H2_VERSION=2.3.232
ADD https://repo1.maven.org/maven2/com/h2database/h2/${H2_VERSION}/h2-${H2_VERSION}.jar /opt/h2.jar
RUN mkdir -p /data && chmod 777 /data
EXPOSE 9092 5435 8082
CMD ["java", "-Xmx2g", "-cp", "/opt/h2.jar", "org.h2.tools.Server", "-tcp", "-tcpAllowOthers", "-tcpPort", "9092", "-pg", "-pgAllowOthers", "-pgPort", "5435", "-web", "-webAllowOthers", "-webPort", "8082", "-baseDir", "/data", "-ifNotExists"]
