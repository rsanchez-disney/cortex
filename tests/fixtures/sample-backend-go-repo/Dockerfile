FROM golang:1.22-alpine AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /app/events-service ./main.go

FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=builder /app/events-service /events-service

USER nonroot:nonroot

EXPOSE 8080

ENTRYPOINT ["/events-service"]
