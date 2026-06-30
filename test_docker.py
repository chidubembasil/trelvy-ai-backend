import docker

client = docker.from_env()
output = client.containers.run("python:3.12-slim", "echo hello from sandbox", remove=True)
print(output)