package main

import (
	"log"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"github.com/example/events-service/internal/handlers"
	"github.com/example/events-service/internal/kafka"
)

func main() {
	r := chi.NewRouter()
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)

	// Health check
	r.Get("/healthz", handlers.HealthCheck)

	// API v1 routes
	r.Route("/api/v1/events", func(r chi.Router) {
		r.Get("/", handlers.ListEvents)
		r.Post("/", handlers.CreateEvent)
		r.Route("/{id}", func(r chi.Router) {
			r.Get("/", handlers.GetEvent)
			r.Put("/", handlers.UpdateEvent)
			r.Delete("/", handlers.DeleteEvent)
		})
	})

	// Start Kafka producer
	producer := kafka.NewProducer()
	defer producer.Close()

	log.Println("Starting server on :8080")
	if err := http.ListenAndServe(":8080", r); err != nil {
		log.Fatal(err)
	}
}
