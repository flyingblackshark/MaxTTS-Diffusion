ARG JAX_STABLE_STACK_BASEIMAGE

# JAX Stable Stack Base Image
FROM $JAX_STABLE_STACK_BASEIMAGE

ARG COMMIT_HASH

ENV COMMIT_HASH=$COMMIT_HASH

RUN mkdir -p /deps

# Set the working directory in the container
WORKDIR /deps

# Copy all files from local workspace into docker container
COPY . .

# Install Maxdiffusion jax stable stack requirements
RUN pip install -r /deps/requirements_with_jax_stable_stack.txt

# Run the script available in JAX-Stable-Stack base image to generate the manifest file
RUN bash /jax-stable-stack/generate_manifest.sh PREFIX=maxdiffusion COMMIT_HASH=$COMMIT_HASH