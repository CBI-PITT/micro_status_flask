A web interface to monitor acquisition and processing of RSCM and MesoSPIM datasets. <br/>

Works with the database populated by RSCM slack bot (see microstatus repo). <br/>
Allows authenticated users to display the Dataset table (filtered by PI and time), edit records, create new records. Deletion is currently not allowed. <br/>
Authentication is performed via LDAP <br/>

### Run on slogin
- ssh lab@slogin.cbiserver.pitt.edu <br/>
- conda activate microstatus-flask <br/>
- cd ~/src/micro_status_flask/micro_status_flask <br/>
- python app.py <br/>


The app can be accessed at slogin.cbiserver.pitt.edu:1414
