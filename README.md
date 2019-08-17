# corocoro
Execute something while controlling to deregister and register from ELB.

## Image

![image](corocoro_image.png)

1. Deregister from CLB/TargetGroup
2. Execute something (such as restart instance)
3. Register to CLB/TargetGroup
4. Do step 1~3 for next instance


# How to use
## Restart instances

```
python corocoro.py restart i-XXXXXXXXXXX i-XXXXXXXXXXXXX i-XXXXXXXXXXXXX 
[INFO]TargetInstance: ['test1', 'test2', 'test3']
Confirm? (yes/no):
>>> yes
...
...
...
```

## Execute RuuCommand
Work in progress...

## Setting

- `DEREGISTER_TIMEOUT` : Timeout seconds to deregister from CLB/Targetgroup
- `REGISTER_TIMEOUT` : Timeout seconds to register to CLB/Targetgroup
- `SLOW_START_WAIT_TIME` : 
- `RESTART_TIMEOUT` : 
