# 6DM Appointment Setter

A full-stack appointment booking and outreach platform for multi-tenant service businesses. This project combines a FastAPI backend, React frontend, PostgreSQL, Redis, and Twilio to manage leads, contacts, bookings, call logs, and SPA onboarding workflows.

## Overview

6DM Appointment Setter helps teams automate and manage the end-to-end appointment lifecycle:

- capture and organize leads and contacts
- route scheduling for multiple SPA accounts
- manage outbound calling flows with Twilio
- log call activity and status
- track appointment booking outcomes
- surface operational metrics in a dashboard

## Tech Stack

### Backend
- Python
- FastAPI
- SQLAlchemy
- PostgreSQL
- Redis
- Twilio
- JWT authentication
- Alembic migrations

### Frontend
- React
- TypeScript
- Vite
- Tailwind CSS
- React Router

### DevOps
- Docker
- Docker Compose

## Repository Structure
.
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── core/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── services/
│   │   └── main.py
│   ├── migrations/
│   ├── tests/
│   ├── requirements.txt
│   ├── pytest.ini
│   └── Dockerfile
├── frontend/
│   ├── src/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
├── package.json
└── README.md
