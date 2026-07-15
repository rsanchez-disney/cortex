package kafka

import (
	"context"
	"log"

	kafkago "github.com/segmentio/kafka-go"
)

const (
	Topic = "events.created"
)

type Producer struct {
	writer *kafkago.Writer
}

func NewProducer() *Producer {
	writer := &kafkago.Writer{
		Addr:     kafkago.TCP("localhost:9092"),
		Topic:    Topic,
		Balancer: &kafkago.LeastBytes{},
	}
	return &Producer{writer: writer}
}

func (p *Producer) Publish(ctx context.Context, key, value []byte) error {
	msg := kafkago.Message{
		Key:   key,
		Value: value,
	}
	err := p.writer.WriteMessages(ctx, msg)
	if err != nil {
		log.Printf("failed to produce message to topic %s: %v", Topic, err)
		return err
	}
	return nil
}

func (p *Producer) Close() error {
	return p.writer.Close()
}
