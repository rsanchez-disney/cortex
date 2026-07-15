package handlers

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
)

type Event struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

func HealthCheck(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(`{"status": "ok"}`))
}

func ListEvents(w http.ResponseWriter, r *http.Request) {
	// Query events from database
	events := []Event{}
	json.NewEncoder(w).Encode(events)
}

func CreateEvent(w http.ResponseWriter, r *http.Request) {
	var event Event
	if err := json.NewDecoder(r.Body).Decode(&event); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	// Save to database and publish to Kafka
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(event)
}

func GetEvent(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	// Fetch event by ID
	event := Event{ID: id, Name: "Sample Event"}
	json.NewEncoder(w).Encode(event)
}

func UpdateEvent(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	var event Event
	if err := json.NewDecoder(r.Body).Decode(&event); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	event.ID = id
	json.NewEncoder(w).Encode(event)
}

func DeleteEvent(w http.ResponseWriter, r *http.Request) {
	_ = chi.URLParam(r, "id")
	w.WriteHeader(http.StatusNoContent)
}
