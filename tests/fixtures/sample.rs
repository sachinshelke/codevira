/// Sample Rust module for testing tree-sitter parsing.
/// Demonstrates functions, structs, enums, traits, and impl blocks.

use std::collections::HashMap;
use std::fmt;

/// A user in the system.
pub struct User {
    pub name: String,
    pub email: String,
}

/// Possible user roles.
pub enum Role {
    Admin,
    Editor,
    Viewer,
}

/// Trait for displayable entities.
pub trait Displayable {
    fn display(&self) -> String;
}

/// Implementation of Displayable for User.
impl Displayable for User {
    fn display(&self) -> String {
        format!("{} <{}>", self.name, self.email)
    }
}

/// Greets a user by name.
pub fn greet(name: &str) -> String {
    format!("Hello, {}!", name)
}

fn _private_helper() -> i32 {
    42
}
