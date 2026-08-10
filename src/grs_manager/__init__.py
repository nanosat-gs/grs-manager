"""GRS Manager: adapter entre o Satellite Tracker (gpredict) e o Station Manager.

Fala rotctld (hamlib) com o gpredict de um lado, e ZMQ com o Station Manager
(mgm8) do outro — o mesmo papel que o Rotor Manager tem entre o Station
Manager e o Rotor Controller físico, só que do lado do Control Desktop.
"""
