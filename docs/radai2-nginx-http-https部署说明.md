# radai2.cdut.edu.cn HTTP/HTTPS 部署说明

## 当前运行方式

`radai2.cdut.edu.cn` 当前由 Docker 容器 `cdut-domain-nginx` 提供 Nginx 服务，容器启动命令为：

```text
/docker-entrypoint.sh nginx -g "daemon off;"
```

容器默认使用 `nginx:1.31.3-alpine`。如需替换为更新的兼容版本，可在启动脚本前设置 `NGINX_IMAGE`。
版本升级需重建容器；单独执行 `docker restart` 只会重启现有容器，
不会更换镜像。

运行中的容器实际加载的配置文件是：

```text
/home/inspur/myenv/langchian_project/backend/scripts/.domain_gateway/radai2.cdut.edu.cn.conf
```

该文件以只读方式挂载到容器内的：

```text
/etc/nginx/conf.d/default.conf
```

`/home/inspur/myenv/langchian_project/backend/scripts/nginx/radai2.cdut.edu.cn.conf` 是项目中的备用配置副本，不是当前容器直接挂载的文件。修改 Nginx 时应优先修改实际挂载的 `.domain_gateway` 配置，并同步更新备用副本。

## HTTP 重定向到 HTTPS

80 端口只负责把请求重定向到 HTTPS，页面和接口由 443 端口处理：

```nginx
server {
    listen 80;
    server_name radai2.cdut.edu.cn;

    return 301 https://radai2.cdut.edu.cn$request_uri;
}
```

因此访问：

```text
http://radai2.cdut.edu.cn
```

会跳转到：

```text
https://radai2.cdut.edu.cn
```

原请求路径和查询参数会通过 `$request_uri` 保留。

## 修改配置后的检查与重启

在宿主机执行：

```bash
cd /home/inspur/myenv/langchian_project

# 检查容器内实际加载的 Nginx 配置
docker exec cdut-domain-nginx nginx -t

# 检查通过后重启容器，使挂载的配置立即生效
docker restart cdut-domain-nginx

# 重启后再次确认配置有效
docker exec cdut-domain-nginx nginx -t
```

不要使用 `systemctl restart nginx` 作为本服务的重启方式。宿主机的 `nginx.service` 不是当前服务来源，80 和 443 端口已经由 `cdut-domain-nginx` 容器占用。

## 验证重定向

建议绕过本机代理进行验证：

```bash
curl --noproxy '*' -I http://radai2.cdut.edu.cn
curl --noproxy '*' -I https://radai2.cdut.edu.cn
```

HTTP 预期返回 `301 Moved Permanently`，并包含：

```text
Location: https://radai2.cdut.edu.cn/
```

HTTPS 应返回正常的 `200` 响应或应用自身的合法跳转。如果浏览器仍显示 `502 Bad Gateway`，而上述直连检查正常，优先检查浏览器、VPN 或校园网 HTTP 代理设置。
