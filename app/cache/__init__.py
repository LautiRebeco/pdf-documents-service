"""Caché de consultas con degradación graceful.

Si ``REDIS_URL`` está configurada se usa Redis (apto para múltiples réplicas
detrás de Traefik). Si no, se usa una caché local en memoria. En ambos casos,
un fallo del backend nunca rompe la petición: se sirve desde MongoDB.
"""
