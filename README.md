# rabbit-tunnel-server

A python-based server implementation for rabbit-tunnel.

```sh
rt-server -sd rtunnel.io
```


### Requirements
- Python 3.7 - 3.9


### Current Limitations

- No unit tests
- No public APIs for control
- No authentification
- No horizontal scaling

Reverse proxy such as nginx is highly recommended to overcome the limitation below. The example configuration of nginx webserver is available at `assets/nginx.conf`.

- Robustness to malformed HTTP requests
- HTTP/2
- <sup>1</sup> Better handling of HTTP keep-alive session
- <sup>2</sup> HTTP response modification

<sup>1</sup> `rabbit-tunnel-server` uses the host header in HTTP requests to determine the target `rabbit-tunnel` instance, but in a HTTP keep-alive session, multiple HTTP requests with different host header can come in the same TCP connection. In that case, `rabbit-tunnel-server` uses the first HTTP request to determinte the target `rabbit-tunnel` instance and assume that aubsequent HTTP requests will have the same target. To overcome this limitation, you can set-up the reverse proxy to initiate new TCP connection when the host header is changed, or even can disable HTTP keep-alive session between the reverse proxy and `rabbit-tunnel-server` (which is the default behavior of reverse proxies such as `nginx`).

<sup>2</sup> `rabbit-tunnel-server` does not interpret any HTTP responses and proxies them as it is. Therefore, for example, the host header in HTTP response may not have the value that clients requested unless tunneled webserver explicitly set them. To overcome this limitation, you can set-up the reverse proxy to properly modify the host header.


### Protocol Specification

TBD