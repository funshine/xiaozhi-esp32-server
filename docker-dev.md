## 1. 安装docker



## 运行Xiaozhi-Server


### 1. 运行容器

```
# cd main\xiaozhi-server
# $currentPath = (Get-Location).Path
# docker run -it --name xiaozhi-server-dev --restart always -e TZ=Asia/Shanghai --security-opt seccomp:unconfined -p 8000:8000 -v "${currentPath}:/app" -w /app kalicyh/python:xiaozhi
# pip install -r requirements.txt
```

### 2. 进入容器
```
# docker exec -it xiaozhi-server-dev bash
```

### 3. 运行程序
```
# python app.py
```

### 4. 退出容器
```
# exit
```

## 运行后台API服务

### 1. 运行mysql
```
# docker run --name xiaozhi-server-db -e MYSQL_ROOT_PASSWORD=123456 -p 3306:3306 -e MYSQL_DATABASE=xiaozhi_esp32_server -e MYSQL_INITDB_ARGS="--character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci" -d mysql:latest
```

### 2. 运行redis
```
# docker run --name xiaozhi-server-redis -p 6379:6379 -d redis:latest
```

### 3. 修改配置文件

在`main/manager-api/src/main/resources/application-dev.yml`中配置数据库连接信息

```
spring:
  datasource:
      #MySQL
      driver-class-name: com.mysql.cj.jdbc.Driver
      url: jdbc:mysql://127.0.0.1:3306/xiaozhi_esp32_server?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai&nullCatalogMeansCurrent=true
      username: root
      password: 123456
```

在`main/manager-api/src/main/resources/application-dev.yml`中配置Redis连接信息

```
spring:
    data:
      redis:
        host: localhost
        port: 6379
        password:
        database: 0
```


### 4. 构建manager-api工程
```
# cd main\manager-api
# $currentPath = (Get-Location).Path
# docker run -it --rm --name xiaozhi-server-api-maven -e TZ=Asia/Shanghai -v "${currentPath}:/app" -w /app maven:3-eclipse-temurin-21-alpine mvn clean install
```

### 5a. 创建manager-api容器
```
# cd main\manager-api
# $currentPath = (Get-Location).Path
# docker run -it --name xiaozhi-server-api --restart always -e TZ=Asia/Shanghai --security-opt seccomp:unconfined -p 8002:8002 -v "${currentPath}:/app" -w /app eclipse-temurin:21-jdk-jammy
```

### 5b. 创建包含maven构建工具的manager-api容器
```
# cd main\manager-api
# $currentPath = (Get-Location).Path
# docker run -it --name xiaozhi-server-api --restart always -e TZ=Asia/Shanghai --security-opt seccomp:unconfined -p 8002:8002 -v "${currentPath}:/app" -w /app maven:3-eclipse-temurin-21-alpine /bin/bash
# docker exec -it xiaozhi-server-api bash
# cd /app
# mvn clean install
```

### 6. 运行manager-api服务
```
# docker exec -it xiaozhi-server-api bash
# cd /app
# java -Duser.timezone=Asia/Shanghai -jar target/xiaozhi-esp32-api.jar
```

## 运行后台WEB服务

### 1. 创建manager-web容器

```
# cd main\manager-web
# $currentPath = (Get-Location).Path
# docker run -it --name xiaozhi-server-web --restart always -e TZ=Asia/Shanghai --security-opt seccomp:unconfined -p 8001:8001 -v "${currentPath}:/app" -w /app node:18
```

### 2. 修改配置文件
在`main/manager-web/vue.config.js`中配置后台API服务地址
```
proxy: {
      '/xiaozhi': {
        target: 'http://127.0.0.1:8002',
        changeOrigin: true
      }
    }
```

### 3. 进入manager-web容器
```
# docker exec -it xiaozhi-server-web bash
```

### 4. 安装依赖
```
# npm install
```

### 5. 运行manager-web程序
```
npm run serve
```

### 6. 退出容器
```
# exit
```
