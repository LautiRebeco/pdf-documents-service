"""Primitivas de resiliencia reutilizables por el servicio.

Este paquete no conoce nada de documentos ni de MongoDB: sólo provee
mecanismos (circuit breaker, bulkhead, reintentos y métricas) que los
servicios de dominio compositor según convenga.
"""
