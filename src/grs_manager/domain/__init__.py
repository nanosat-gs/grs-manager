"""Modelo e portas do GRS Manager — independentes do mgm8.

GRS Manager e Station Manager são processos separados que só se falam por
ZMQ (fronteira de rede). Por isso o GRS Manager tem seu próprio value object
de posição (`RotorPosition`), em vez de importar `AntennaPosition` do mgm8.
"""
