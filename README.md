# Caching Proxy CLI Tool

*caching-proxy is a CLI tool that starts a caching proxy server, it will forward requests to the actual server and cache the responses.*

## Usage
+ `caching-proxy --port 5000 --origin http://dummyjson.com`
+ `caching-proxy --clear-cache`

## Example
- Open a terminal and run `caching-proxy --port 5000 --origin http://dummyjson.com`
- Open another terminal and test if it's working `curl -i http://127.0.0.1:5000/products`

*Make sure to install poetry `pipx install poetry` and run `poetry install` to use the tool.*