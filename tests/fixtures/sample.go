// Package sample provides test fixtures for tree-sitter Go parsing.
// It demonstrates functions, methods, structs, and interfaces.
package sample

import (
	"fmt"
	"net/http"
)

// UserService manages user operations.
// It provides CRUD functionality.
type UserService struct {
	Name  string
	Email string
}

// GetName returns the user's display name.
func (s *UserService) GetName() string {
	return s.Name
}

// unexportedMethod is private to the package.
func (s *UserService) unexportedMethod() {
	// internal logic
}

// Handler is the HTTP handler interface.
type Handler interface {
	ServeHTTP(w http.ResponseWriter, r *http.Request)
}

// Greet returns a greeting message for the given name.
func Greet(name string) string {
	return fmt.Sprintf("Hello, %s!", name)
}

// helper is not exported.
func helper() int {
	return 42
}
