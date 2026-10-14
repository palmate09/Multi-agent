from fastapi import FastAPI
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()
app = FastAPI()


class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True)
    isbn = Column(String)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/books")
def books() -> list:
    return []
